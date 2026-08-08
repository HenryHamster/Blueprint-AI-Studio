"""Tests for the standalone size-guardrail checker (check_size_limits.py).

Covers: check mode flags a real breach (non-zero exit); check mode passes when
clean; --fix bumps a breached cap to exactly bump_size_limit(current) and leaves
other entries untouched; the rewritten JSON preserves structure and re-checks
clean; section scoping works; absent-tree sections are skipped not errored.
"""

import json
import os
import subprocess
import sys

import app.size_limit_policy as slp
from app.size_limit_policy import bump_size_limit
from testing import check_size_limits as csl


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


def _write_lines(path: str, n: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(f"line {i}" for i in range(n)))
        f.write("\n")


def _build_repo(tmp_path, *, backend=None, agent=None, frontend=None):
    """Create a fake repo tree + a module_size_limits.json.

    Each of backend/agent/frontend is a dict ``{rel_path: (line_count, cap)}``.
    If a section is None its source tree is NOT created (to exercise skips), but
    the JSON still lists it. Returns (repo_root, json_path).
    """
    repo = tmp_path / "repo"
    limits: dict = {"_doc": "test doc — do not ratchet down"}

    section_roots = {
        "backend": repo / "backend" / "app",
        "agent": repo / "agent" / "src",
        "frontend": repo / "admin-ui" / "src",
    }
    for section, spec in (
        ("backend", backend),
        ("agent", agent),
        ("frontend", frontend),
    ):
        limits[section] = {}
        if spec is None:
            continue
        root = section_roots[section]
        for rel, (count, cap) in spec.items():
            _write_lines(str(root / rel), count)
            limits[section][rel] = cap

    json_path = repo / "backend" / "testing" / "module_size_limits.json"
    os.makedirs(os.path.dirname(str(json_path)), exist_ok=True)
    with open(json_path, "w") as f:
        json.dump(limits, f, indent=2, ensure_ascii=True)
        f.write("\n")
    return str(repo), str(json_path)


def _run(json_path, repo_root, *extra):
    return csl.main(["--json", json_path, "--repo-root", repo_root, *extra])


def test_check_flags_real_breach_nonzero(tmp_path):
    repo, jp = _build_repo(
        tmp_path,
        backend={"big.py": (120, 100), "ok.py": (50, 100)},
    )
    assert _run(jp, repo, "--sections", "backend") == 1


def test_check_passes_when_clean(tmp_path):
    repo, jp = _build_repo(
        tmp_path,
        backend={"a.py": (50, 100), "b.py": (99, 100)},
    )
    assert _run(jp, repo, "--sections", "backend") == 0


def test_fix_bumps_only_breached_cap_to_policy_value(tmp_path):
    repo, jp = _build_repo(
        tmp_path,
        backend={"big.py": (1720, 1600), "ok.py": (50, 100)},
    )
    assert _run(jp, repo, "--fix", "--sections", "backend") == 0

    data = json.loads(_read(jp))
    # Breached file bumped to exactly the canonical policy value.
    assert data["backend"]["big.py"] == bump_size_limit(1720)
    assert data["backend"]["big.py"] == 2000
    # Untouched file keeps its original cap.
    assert data["backend"]["ok.py"] == 100
    # And re-running check now passes.
    assert _run(jp, repo, "--sections", "backend") == 0


def test_fix_preserves_structure_and_key_order(tmp_path):
    repo, jp = _build_repo(
        tmp_path,
        backend={"z_first.py": (300, 100), "a_second.py": (50, 100)},
        agent={"index.ts": (10, 100)},
        frontend={"App.tsx": (10, 100)},
    )
    before = json.loads(_read(jp))
    before_top_order = list(before.keys())
    before_backend_order = list(before["backend"].keys())

    assert _run(jp, repo, "--fix") == 0

    after = json.loads(_read(jp))
    # Top-level _doc + section ordering preserved.
    assert list(after.keys()) == before_top_order
    assert before_top_order[0] == "_doc"
    assert after["_doc"] == before["_doc"]
    # Insertion order within the backend section preserved (not re-sorted).
    assert list(after["backend"].keys()) == before_backend_order
    # File ends with a trailing newline.
    assert _read(jp).endswith("}\n")


def test_section_scoping(tmp_path):
    # Breach lives only in the agent section.
    repo, jp = _build_repo(
        tmp_path,
        backend={"ok.py": (50, 100)},
        agent={"big.ts": (200, 100)},
    )
    # Scoping to backend ignores the agent breach -> clean.
    assert _run(jp, repo, "--sections", "backend") == 0
    # Scoping to agent surfaces it -> non-zero.
    assert _run(jp, repo, "--sections", "agent") == 1


def test_absent_tree_section_skipped_not_errored(tmp_path, capsys):
    # Only backend tree exists; agent + frontend trees are absent.
    repo, jp = _build_repo(
        tmp_path,
        backend={"ok.py": (50, 100)},
        agent=None,
        frontend=None,
    )
    # Absent sections are skipped (reported), not errored — clean exit.
    rc = _run(jp, repo, "--sections", "agent,frontend")
    assert rc == 0
    out = capsys.readouterr().out
    assert "[skip] agent" in out
    assert "[skip] frontend" in out


def test_default_sections_check_all(tmp_path):
    repo, jp = _build_repo(
        tmp_path,
        backend={"ok.py": (50, 100)},
        agent={"big.ts": (300, 100)},
        frontend={"App.tsx": (10, 100)},
    )
    # No --sections flag => all sections; the agent breach must surface.
    assert _run(jp, repo) == 1


# ── Uncapped-file (coverage meta-test) remediation ──────────────────────────


def test_check_flags_uncapped_over_threshold_nonzero(tmp_path):
    # An oversized backend file with NO cap entry must fail check mode.
    repo, jp = _build_repo(tmp_path, backend={"ok.py": (50, 100)})
    _write_lines(str(tmp_path / "repo" / "backend" / "app" / "huge.py"), 1600)
    rc = _run(jp, repo, "--sections", "backend")
    assert rc == 1


def test_check_ignores_uncapped_under_threshold(tmp_path):
    # A file with no cap but under the coverage threshold is fine.
    repo, jp = _build_repo(tmp_path, backend={"ok.py": (50, 100)})
    _write_lines(str(tmp_path / "repo" / "backend" / "app" / "small.py"), 1499)
    assert _run(jp, repo, "--sections", "backend") == 0


def test_fix_adds_cap_for_uncapped_file_at_policy_value(tmp_path):
    repo, jp = _build_repo(tmp_path, backend={"ok.py": (50, 100)})
    _write_lines(str(tmp_path / "repo" / "backend" / "app" / "huge.py"), 1600)

    assert _run(jp, repo, "--fix", "--sections", "backend") == 0

    data = json.loads(_read(jp))
    # New cap added, sized at exactly the canonical policy headroom.
    assert csl.recommended_cap_for_line_count(1600) == bump_size_limit(1600)
    assert data["backend"]["huge.py"] == csl.recommended_cap_for_line_count(1600)
    # The pre-existing entry is untouched.
    assert data["backend"]["ok.py"] == 100
    # And re-running check now passes.
    assert _run(jp, repo, "--sections", "backend") == 0


def test_fix_bumps_and_adds_in_one_pass(tmp_path):
    # One section with both a breached existing cap and an uncapped large file.
    repo, jp = _build_repo(tmp_path, backend={"big.py": (1720, 1600)})
    _write_lines(str(tmp_path / "repo" / "backend" / "app" / "huge.py"), 1600)

    assert _run(jp, repo, "--fix", "--sections", "backend") == 0

    data = json.loads(_read(jp))
    assert data["backend"]["big.py"] == bump_size_limit(1720)
    assert data["backend"]["huge.py"] == bump_size_limit(1600)
    assert _run(jp, repo, "--sections", "backend") == 0


def test_fix_no_op_message_when_nothing_to_do(tmp_path, capsys):
    # No breach + no uncapped file => the no-op message the agent's loop keys on.
    repo, jp = _build_repo(tmp_path, backend={"ok.py": (50, 100)})
    assert _run(jp, repo, "--fix", "--sections", "backend") == 0
    out = capsys.readouterr().out
    assert "No breaches to fix" in out


def test_fix_add_prints_added_not_no_op(tmp_path, capsys):
    # When only an add happens, the output must NOT say "No breaches to fix"
    # (the agent's deterministic loop treats that string as a no-op).
    repo, jp = _build_repo(tmp_path, backend={"ok.py": (50, 100)})
    _write_lines(str(tmp_path / "repo" / "backend" / "app" / "huge.py"), 1600)
    assert _run(jp, repo, "--fix", "--sections", "backend") == 0
    out = capsys.readouterr().out
    assert "No breaches to fix" not in out
    assert "Added 1 cap(s)" in out


def test_agent_test_and_dts_files_excluded_from_uncapped_scan(tmp_path):
    # .test.ts and .d.ts agent files mirror test_invariants._is_agent_excluded —
    # even when oversized they must not be flagged as uncapped. Call the scanner
    # directly so the assertion is independent of which source trees happen to be
    # mounted in the test environment (agent/src, /admin-ui/src).
    root = tmp_path / "src"
    _write_lines(str(root / "huge.test.ts"), 1600)
    _write_lines(str(root / "types.d.ts"), 1600)
    _write_lines(str(root / "real.ts"), 1600)  # the one genuine offender
    offenders = csl.scan_uncapped_section(
        str(root), {}, (".ts",), csl._is_agent_excluded
    )
    assert [rec["rel"] for rec in offenders] == ["real.ts"]


def test_frontend_test_files_excluded_from_uncapped_scan(tmp_path):
    # Frontend test/type files mirror test_invariants._is_frontend_excluded.
    root = tmp_path / "src"
    _write_lines(str(root / "Big.test.tsx"), 1600)
    _write_lines(str(root / "Big.test.ts"), 1600)
    _write_lines(str(root / "vite-env.d.ts"), 1600)
    _write_lines(str(root / "Real.tsx"), 1600)  # the one genuine offender
    offenders = csl.scan_uncapped_section(
        str(root), {}, (".ts", ".tsx"), csl._is_frontend_excluded
    )
    assert [rec["rel"] for rec in offenders] == ["Real.tsx"]


def test_explicit_repo_root_uncapped_scan_ignores_admin_ui_src_override(
    tmp_path, monkeypatch, capsys
):
    repo, jp = _build_repo(tmp_path, frontend={"App.tsx": (10, 100)})
    override_root = tmp_path / "admin-ui-override" / "src"
    _write_lines(str(override_root / "Leaked.tsx"), 1600)
    monkeypatch.setenv("ADMIN_UI_SRC", str(override_root))

    rc = _run(jp, repo, "--sections", "frontend")

    assert rc == 0
    out = capsys.readouterr().out
    assert "Leaked.tsx" not in out
    assert "[uncapped]" not in out


def test_cli_explicit_repo_root_ignores_admin_ui_src_override(
    tmp_path, monkeypatch, capsys
):
    repo, jp = _build_repo(tmp_path, frontend={"App.tsx": (10, 100)})
    override_root = tmp_path / "admin-ui-override" / "src"
    _write_lines(str(override_root / "Leaked.tsx"), 1600)
    monkeypatch.setenv("ADMIN_UI_SRC", str(override_root))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_size_limits.py",
            "--json",
            jp,
            "--repo-root",
            repo,
            "--sections",
            "frontend",
        ],
    )

    rc = csl.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "Leaked.tsx" not in out
    assert "[uncapped]" not in out


def test_uncapped_scan_skips_already_capped_files(tmp_path):
    # A file already present in caps is not an "uncapped" offender even if large.
    root = tmp_path / "src"
    _write_lines(str(root / "capped.py"), 1600)
    _write_lines(str(root / "uncapped.py"), 1600)
    offenders = csl.scan_uncapped_section(
        str(root), {"capped.py": 1700}, (".py",), csl._no_exclusion
    )
    assert [rec["rel"] for rec in offenders] == ["uncapped.py"]


# ── Merge-duplicated bump-pair dedup (cap_bump_history generator) ─────────────


def test_parse_git_diff_dedups_merge_duplicated_bump_pairs():
    """Merge-duplicated bump pairs collapse to the earliest-dated commit.

    The same cap change (old_cap -> new_cap) appears twice in ``git log --all``
    output — once on the commit that first introduced the new limit value and
    again on a sibling/promotion commit that re-applied the identical value.
    git log emits commits newest-first, so the later duplicate appears FIRST in
    the output. The dedup must keep the earliest-dated record of each pair by
    timestamp (not position), and must not collapse distinct later bumps.
    """
    log = """\
COMMIT bbb222 2026-05-22T22:49:42+00:00

diff --git a/backend/testing/module_size_limits.json b/backend/testing/module_size_limits.json
--- a/backend/testing/module_size_limits.json
+++ b/backend/testing/module_size_limits.json
@@ -1,5 +1,5 @@
   "backend": {
-    "main.py": 3700,
+    "main.py": 3800,
   }

COMMIT aaa111 2026-05-22T03:27:04+00:00

diff --git a/backend/testing/module_size_limits.json b/backend/testing/module_size_limits.json
--- a/backend/testing/module_size_limits.json
+++ b/backend/testing/module_size_limits.json
@@ -1,5 +1,5 @@
   "backend": {
-    "main.py": 3700,
+    "main.py": 3800,
   }

COMMIT ccc333 2026-06-01T10:00:00+00:00

diff --git a/backend/testing/module_size_limits.json b/backend/testing/module_size_limits.json
--- a/backend/testing/module_size_limits.json
+++ b/backend/testing/module_size_limits.json
@@ -1,5 +1,5 @@
   "backend": {
-    "main.py": 3800,
+    "main.py": 3900,
   }
"""
    records = slp._parse_git_diff_output(log, "main.py")
    # (3700, 3800) appeared twice — collapsed to the earlier commit (aaa111),
    # even though the later duplicate (bbb222) appears first in the output.
    # (3800, 3900) is a distinct later bump and survives.
    assert [(r["commit_sha"], r["old_cap"], r["new_cap"]) for r in records] == [
        ("aaa111", 3700, 3800),
        ("ccc333", 3800, 3900),
    ]


def test_parse_git_diff_does_not_dedup_distinct_pairs():
    """A module's distinct bumps all survive; only identical pairs collapse."""
    log = """\
COMMIT bbb222 2026-06-01T10:00:00+00:00

diff --git a/backend/testing/module_size_limits.json b/backend/testing/module_size_limits.json
--- a/backend/testing/module_size_limits.json
+++ b/backend/testing/module_size_limits.json
@@ -1,5 +1,5 @@
   "backend": {
-    "m.py": 1000,
+    "m.py": 1200,
   }

COMMIT aaa111 2026-05-01T10:00:00+00:00

diff --git a/backend/testing/module_size_limits.json b/backend/testing/module_size_limits.json
--- a/backend/testing/module_size_limits.json
+++ b/backend/testing/module_size_limits.json
@@ -1,5 +1,5 @@
   "backend": {
-    "m.py": 800,
+    "m.py": 1000,
   }
"""
    records = slp._parse_git_diff_output(log, "m.py")
    assert [(r["commit_sha"], r["old_cap"], r["new_cap"]) for r in records] == [
        ("aaa111", 800, 1000),
        ("bbb222", 1000, 1200),
    ]


def test_parse_git_diff_dedup_keeps_earliest_across_offsets():
    """The earliest survivor is chosen by actual timestamp, not ISO-string order.

    ``-07:00`` is later in the day than ``+00:00`` for the same clock time; a
    raw string sort would mis-order ``22:00-07:00`` (== 05:00 UTC next day)
    before ``00:30+00:00``. Dedup must treat them by their true UTC instant.
    """
    log = """\
COMMIT aaa111 2026-05-22T00:30:00+00:00

diff --git a/backend/testing/module_size_limits.json b/backend/testing/module_size_limits.json
--- a/backend/testing/module_size_limits.json
+++ b/backend/testing/module_size_limits.json
@@ -1,5 +1,5 @@
   "backend": {
-    "main.py": 3700,
+    "main.py": 3800,
   }

COMMIT bbb222 2026-05-21T22:00:00-07:00

diff --git a/backend/testing/module_size_limits.json b/backend/testing/module_size_limits.json
--- a/backend/testing/module_size_limits.json
+++ b/backend/testing/module_size_limits.json
@@ -1,5 +1,5 @@
   "backend": {
-    "main.py": 3700,
+    "main.py": 3800,
   }
"""
    records = slp._parse_git_diff_output(log, "main.py")
    # bbb222 (-07:00) is 2026-05-22T05:00:00+00:00 — LATER than aaa111's
    # 00:30+00:00, so aaa111 wins even though "22:00-07:00" < "00:30+00:00"
    # as a raw string.
    assert len(records) == 1
    assert records[0]["commit_sha"] == "aaa111"


# ── Chronic-offender gate in --fix mode ─────────────────────────────────────


def test_chronic_offender_not_silently_fixed(tmp_path, capsys, mocker):
    """Primary acceptance criterion: --fix must NOT silently bump a chronic offender's cap.

    Selector: test_size_limits_checker.py::test_chronic_offender_not_silently_fixed
    """
    from datetime import datetime, timedelta, timezone

    from app.size_limit_policy import CHRONIC_WINDOW_DAYS, BumpRecord

    repo, jp = _build_repo(
        tmp_path,
        backend={"chronic.py": (1720, 1600), "clean.py": (50, 100)},
    )

    # Simulate 3 recent bumps — enough to trigger chronic classification.
    def _rec(old, new, days_ago):
        date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        return BumpRecord(
            commit_sha=f"sha{days_ago}", commit_date=date, old_cap=old, new_cap=new
        )

    chronic_history = [
        _rec(1000, 1200, CHRONIC_WINDOW_DAYS - 10),
        _rec(1200, 1400, CHRONIC_WINDOW_DAYS - 60),
        _rec(1400, 1600, 30),
    ]

    with mocker.patch(
        "app.size_limit_policy.parse_bump_history", return_value=chronic_history
    ):
        rc = _run(jp, repo, "--fix", "--sections", "backend")

    out = capsys.readouterr().out

    # Must exit 1 to signal that a chronic offender was skipped.
    assert rc == 1, f"Expected exit 1 for chronic offender but got {rc}"

    # Must print [chronic] tag identifying the skipped file.
    assert "[chronic]" in out, f"Expected '[chronic]' in output but got:\n{out}"
    assert "chronic.py" in out

    # The JSON cap must NOT have been bumped — the file stays at 1600.
    import json

    data = json.loads(_read(jp))
    assert data["backend"]["chronic.py"] == 1600, (
        f"Chronic offender cap should not be bumped; got {data['backend']['chronic.py']}"
    )

    # The clean (non-breaching) file must be untouched.
    assert data["backend"]["clean.py"] == 100


def test_first_time_breach_still_bumped_normally(tmp_path, mocker):
    """A first-time breach (empty history) must still be bumped as usual."""
    repo, jp = _build_repo(tmp_path, backend={"new.py": (1720, 1600)})

    with mocker.patch("app.size_limit_policy.parse_bump_history", return_value=[]):
        rc = _run(jp, repo, "--fix", "--sections", "backend")

    assert rc == 0

    import json

    data = json.loads(_read(jp))
    assert data["backend"]["new.py"] == bump_size_limit(1720)


def test_fix_chronic_git_read_failure_refuses_bump(tmp_path, capsys, mocker):
    """If the git history read fails in a real checkout, --fix must FAIL CLOSED —
    skip the cap bump and route to decomposition, not degrade to a routine bump.

    Unreadable bump history is NOT the same as "no bumps on record"; a possibly
    chronic offender must not receive a silent routine bump when its history
    cannot be read (e.g. a truncated task-VM clone). The git-failure path is
    only taken when ``_REPO_ROOT`` is a git checkout, so this pins that
    precondition deterministically (in the CI harness ``_REPO_ROOT`` is a
    non-checkout and would otherwise use the ledger fallback).
    """
    # Distinct key from the other real-subprocess tests so the module TTL cache
    # cannot leak a genuinely-empty history into this unreadable-history case.
    slp._bump_history_cache.clear()
    repo, jp = _build_repo(tmp_path, backend={"big_unreadable.py": (1720, 1600)})

    # Pin the real-checkout precondition: git on PATH + _REPO_ROOT is a checkout.
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    mocker.patch("app.size_limit_policy._REPO_ROOT", str(checkout))

    # Simulate git subprocess failing entirely.
    with mocker.patch(
        "app.size_limit_policy.subprocess.run",
        side_effect=FileNotFoundError("git not found"),
    ):
        rc = _run(jp, repo, "--fix", "--sections", "backend")

    # Must refuse the bump (exit 1) and leave the cap untouched.
    assert rc == 1

    import json

    data = json.loads(_read(jp))
    assert data["backend"]["big_unreadable.py"] == 1600, (
        f"Unreadable history must not bump the cap; "
        f"got {data['backend']['big_unreadable.py']}"
    )
    out = capsys.readouterr().out
    assert "[chronic]" in out, (
        f"Expected the unreadable-history breach to be classified as a "
        f"chronic/decomposition refusal, got output:\n{out}"
    )


def test_force_chronic_flag_overrides_chronic_gate(tmp_path, capsys, mocker):
    """--force-chronic grants operator-approved bump even for chronic offenders."""
    from datetime import datetime, timedelta, timezone

    from app.size_limit_policy import CHRONIC_WINDOW_DAYS, BumpRecord

    repo, jp = _build_repo(tmp_path, backend={"chronic.py": (1720, 1600)})

    def _rec(old, new, days_ago):
        date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        return BumpRecord(
            commit_sha=f"sha{days_ago}", commit_date=date, old_cap=old, new_cap=new
        )

    chronic_history = [
        _rec(1000, 1200, CHRONIC_WINDOW_DAYS - 10),
        _rec(1200, 1400, CHRONIC_WINDOW_DAYS - 60),
        _rec(1400, 1600, 30),
    ]

    with mocker.patch(
        "app.size_limit_policy.parse_bump_history", return_value=chronic_history
    ):
        rc = _run(jp, repo, "--fix", "--force-chronic", "--sections", "backend")

    assert rc == 0

    import json

    out = capsys.readouterr().out
    data = json.loads(_read(jp))
    assert data["backend"]["chronic.py"] == bump_size_limit(1720)
    # Should label it as operator-approved, not a silent bump.
    assert "chronic" in out.lower()


def test_coverage_threshold_in_sync_with_test_invariants():
    # The checker mirrors (not imports) the meta-test threshold; guard the drift.
    from testing import test_invariants

    assert csl._COVERAGE_THRESHOLD == test_invariants._COVERAGE_THRESHOLD


# ── --base-root baseline mode ─────────────────────────────────────────────────
# A breach INHERITED from the base branch (same file already over-cap there at
# the same-or-larger size, judged against the base's own limits JSON) must not
# fail the check — it is upstream breakage the current change cannot remediate.
# A breach INTRODUCED here, or one that GREW past its base size, still blocks.


def _build_base_repo(tmp_path, *, backend=None, agent=None, frontend=None):
    """Like _build_repo but under a sibling 'base' dir (a fake base checkout)."""
    base_dir = tmp_path / "base"
    base_dir.mkdir(exist_ok=True)
    return _build_repo(base_dir, backend=backend, agent=agent, frontend=frontend)


def test_base_root_preexisting_breach_warn_passes(tmp_path, capsys):
    repo, jp = _build_repo(tmp_path, backend={"big.py": (900, 800)})
    base_repo, _ = _build_base_repo(tmp_path, backend={"big.py": (900, 800)})

    rc = _run(jp, repo, "--base-root", base_repo, "--sections", "backend")

    out = capsys.readouterr().out
    assert rc == 0
    assert "[pre-existing] backend/big.py" in out
    assert "[breach]" not in out
    # The advisory names the upstream remediation path (surface the REASON).
    assert "chore PR running check_size_limits.py --fix" in out
    assert "--force-chronic" in out


def test_base_root_new_breach_still_blocks(tmp_path, capsys):
    repo, jp = _build_repo(tmp_path, backend={"big.py": (900, 800)})
    base_repo, _ = _build_base_repo(tmp_path, backend={"big.py": (700, 800)})

    rc = _run(jp, repo, "--base-root", base_repo, "--sections", "backend")

    out = capsys.readouterr().out
    assert rc == 1
    assert "[breach] backend/big.py: 900 lines (cap: 800)" in out
    assert "[pre-existing]" not in out


def test_base_root_grown_breach_blocks_with_growth_reason(tmp_path, capsys):
    repo, jp = _build_repo(tmp_path, backend={"big.py": (950, 800)})
    base_repo, _ = _build_base_repo(tmp_path, backend={"big.py": (900, 800)})

    rc = _run(jp, repo, "--base-root", base_repo, "--sections", "backend")

    out = capsys.readouterr().out
    assert rc == 1
    assert "[breach] backend/big.py" in out
    assert "grew from 900 lines on base" in out


def test_base_root_mixed_preexisting_and_new_blocks_on_new_only(tmp_path, capsys):
    repo, jp = _build_repo(
        tmp_path,
        backend={"inherited.py": (900, 800), "fresh.py": (850, 800)},
    )
    base_repo, _ = _build_base_repo(
        tmp_path,
        backend={"inherited.py": (900, 800), "fresh.py": (100, 800)},
    )

    rc = _run(jp, repo, "--base-root", base_repo, "--sections", "backend")

    out = capsys.readouterr().out
    assert rc == 1
    assert "[breach] backend/fresh.py" in out
    assert "[pre-existing] backend/inherited.py" in out


def test_base_root_preexisting_uncapped_warn_passes(tmp_path, capsys):
    over = csl._COVERAGE_THRESHOLD + 100
    repo, jp = _build_repo(tmp_path, backend={"capped.py": (10, 100)})
    _write_lines(os.path.join(repo, "backend", "app", "huge.py"), over)
    base_repo, _ = _build_base_repo(tmp_path, backend={"capped.py": (10, 100)})
    _write_lines(os.path.join(base_repo, "backend", "app", "huge.py"), over)

    rc = _run(jp, repo, "--base-root", base_repo, "--sections", "backend")

    out = capsys.readouterr().out
    assert rc == 0
    assert "[pre-existing] backend/huge.py" in out
    assert "[uncapped]" not in out


def test_base_root_new_uncapped_still_blocks(tmp_path, capsys):
    over = csl._COVERAGE_THRESHOLD + 100
    repo, jp = _build_repo(tmp_path, backend={"capped.py": (10, 100)})
    _write_lines(os.path.join(repo, "backend", "app", "huge.py"), over)
    base_repo, _ = _build_base_repo(tmp_path, backend={"capped.py": (10, 100)})

    rc = _run(jp, repo, "--base-root", base_repo, "--sections", "backend")

    out = capsys.readouterr().out
    assert rc == 1
    assert "[uncapped] backend/huge.py" in out


def test_base_root_unreadable_baseline_fails_closed(tmp_path, capsys):
    repo, jp = _build_repo(tmp_path, backend={"big.py": (900, 800)})
    missing_base = str(tmp_path / "nonexistent-base")

    rc = _run(jp, repo, "--base-root", missing_base, "--sections", "backend")

    out = capsys.readouterr().out
    assert rc == 1
    assert "[baseline] unavailable" in out
    assert "[breach] backend/big.py" in out


def test_base_root_judges_base_against_its_own_caps(tmp_path, capsys):
    # Base file is over the PR's cap but within the BASE's own (higher) cap:
    # the breach is NOT pre-existing — this change tightened the cap, so it
    # owns the result and must block.
    repo, jp = _build_repo(tmp_path, backend={"big.py": (900, 800)})
    base_repo, _ = _build_base_repo(tmp_path, backend={"big.py": (900, 1000)})

    rc = _run(jp, repo, "--base-root", base_repo, "--sections", "backend")

    out = capsys.readouterr().out
    assert rc == 1
    assert "[breach] backend/big.py" in out


def test_base_root_ignored_under_fix(tmp_path):
    repo, jp = _build_repo(tmp_path, backend={"big.py": (900, 800)})
    base_repo, _ = _build_base_repo(tmp_path, backend={"big.py": (900, 800)})

    rc = _run(jp, repo, "--fix", "--base-root", base_repo, "--sections", "backend")

    assert rc == 0
    data = json.loads(_read(jp))
    # --fix still bumps the (pre-existing) breach — the flag only affects check mode.
    assert data["backend"]["big.py"] == bump_size_limit(900)


def test_no_base_root_output_unchanged(tmp_path, capsys):
    # Without --base-root the classic output shape is preserved bit-for-bit
    # (no baseline lines, plain [breach] with no growth suffix).
    repo, jp = _build_repo(tmp_path, backend={"big.py": (900, 800)})

    rc = _run(jp, repo, "--sections", "backend")

    out = capsys.readouterr().out
    assert rc == 1
    assert "[breach] backend/big.py: 900 lines (cap: 800)\n" in out
    assert "grew from" not in out
    assert "[baseline]" not in out
    assert "[pre-existing]" not in out


def test_base_root_breach_not_excused_by_base_uncapped_offender(tmp_path, capsys):
    # Cross-class mixup guard: the file is a capped BREACH on the PR but was an
    # UNCAPPED offender (no cap entry) on base at the identical size. The two
    # baseline maps are class-keyed separately, so the base's uncapped record
    # must NOT excuse the PR-side breach — a mutation merging the two maps
    # would wrongly warn-pass here.
    over = csl._COVERAGE_THRESHOLD + 100
    repo, jp = _build_repo(tmp_path, backend={"big.py": (over, 800)})
    base_repo, _ = _build_base_repo(tmp_path, backend={"other.py": (10, 100)})
    _write_lines(os.path.join(base_repo, "backend", "app", "big.py"), over)

    rc = _run(jp, repo, "--base-root", base_repo, "--sections", "backend")

    out = capsys.readouterr().out
    assert rc == 1
    assert "[breach] backend/big.py" in out
    assert "[pre-existing]" not in out


def test_base_root_uncapped_not_excused_by_base_breach(tmp_path, capsys):
    # Cross-class mixup guard, other direction: the file is an UNCAPPED
    # offender on the PR (its cap entry was removed) but was a capped BREACH on
    # base at the identical size. Must block, not warn.
    over = csl._COVERAGE_THRESHOLD + 100
    repo, jp = _build_repo(tmp_path, backend={"other.py": (10, 100)})
    _write_lines(os.path.join(repo, "backend", "app", "big.py"), over)
    base_repo, _ = _build_base_repo(tmp_path, backend={"big.py": (over, 800)})

    rc = _run(jp, repo, "--base-root", base_repo, "--sections", "backend")

    out = capsys.readouterr().out
    assert rc == 1
    assert "[uncapped] backend/big.py" in out
    assert "[pre-existing]" not in out


# ── Cap-raise guardrail (silent cap raises must fail CHECK mode) ─────────────


def test_detect_unjustified_cap_raises_pure():
    """Pure detection: raises above the MAINLINE baseline are flagged; lowerings,
    unchanged caps, and brand-new caps (no baseline value) never are."""
    mainline = {
        "backend": {"big.py": 100, "same.py": 50, "gone.py": 40},
        "agent": {"a.ts": 10},
    }
    data = {
        # big.py raised 100 -> 200; same.py unchanged; gone.py lowered 40 -> 30
        # (was on mainline but not raised); new.py added with no mainline cap.
        "backend": {"big.py": 200, "same.py": 50, "gone.py": 30, "new.py": 25},
        "agent": {"a.ts": 10},
    }
    raises = csl.detect_unjustified_cap_raises(
        data, mainline, ["backend", "agent"], history_for=lambda s, r: []
    )
    assert raises == [("backend", "big.py", 100, 200)]


def test_detect_unjustified_cap_raises_ledger_justified():
    """A raise with a matching cap-bump-history entry is deliberate and not flagged."""
    raises = csl.detect_unjustified_cap_raises(
        {"backend": {"m.py": 200}},
        {"backend": {"m.py": 100}},
        ["backend"],
        history_for=lambda s, r: [{"old_cap": 100, "new_cap": 200}],
    )
    assert raises == []


def test_detect_unjustified_cap_raises_bumprecord_contract():
    """parse_bump_history returns BumpRecord dataclasses (not dicts); the
    justification check must accept that shape too."""
    from datetime import datetime, timezone

    from app.size_limit_policy import BumpRecord

    record = BumpRecord(
        commit_sha="sha1",
        commit_date=datetime.now(timezone.utc).isoformat(),
        old_cap=100,
        new_cap=200,
    )
    raises = csl.detect_unjustified_cap_raises(
        {"backend": {"m.py": 200}},
        {"backend": {"m.py": 100}},
        ["backend"],
        history_for=lambda s, r: [record],
    )
    assert raises == []
    # And the same record does NOT justify a different (old, new) pair.
    raises = csl.detect_unjustified_cap_raises(
        {"backend": {"m.py": 300}},
        {"backend": {"m.py": 100}},
        ["backend"],
        history_for=lambda s, r: [record],
    )
    assert raises == [("backend", "m.py", 100, 300)]


def test_check_flags_unjustified_cap_raise(tmp_path, capsys, mocker):
    """CHECK must FAIL when a diff raises a cap with no cap_bump_history entry —
    naming the file, the old and new values, and how to record the bump."""
    repo, jp = _build_repo(tmp_path, backend={"m.py": (50, 200)})
    # The mainline ancestry baseline caps m.py at 100; this change raised it to
    # 200. No ledger entry justifies the raise.
    mocker.patch(
        "testing.check_size_limits._mainline_limits_via_git",
        return_value=({"backend": {"m.py": 100}}, None),
    )
    mocker.patch("app.size_limit_policy.parse_bump_history", return_value=[])
    rc = _run(jp, repo, "--sections", "backend")
    assert rc == 1
    out = capsys.readouterr().out
    assert "[cap-raise] backend/m.py: cap raised 100 -> 200" in out
    assert "cap_bump_history.json" in out
    assert "--fix" in out


def test_check_ledger_justified_cap_raise_passes(tmp_path, capsys, mocker):
    """A raise WITH a matching cap_bump_history entry stays legal."""
    from datetime import datetime, timezone

    from app.size_limit_policy import BumpRecord

    repo, jp = _build_repo(tmp_path, backend={"m.py": (50, 200)})
    mocker.patch(
        "testing.check_size_limits._mainline_limits_via_git",
        return_value=({"backend": {"m.py": 100}}, None),
    )
    mocker.patch(
        "app.size_limit_policy.parse_bump_history",
        return_value=[
            BumpRecord(
                commit_sha="sha1",
                commit_date=datetime.now(timezone.utc).isoformat(),
                old_cap=100,
                new_cap=200,
            )
        ],
    )
    assert _run(jp, repo, "--sections", "backend") == 0


def test_check_cap_lowering_always_legal(tmp_path, capsys, mocker):
    """Lowering a cap must never require a ledger entry — even with empty history."""
    repo, jp = _build_repo(tmp_path, backend={"m.py": (50, 100)})
    mocker.patch(
        "testing.check_size_limits._mainline_limits_via_git",
        return_value=({"backend": {"m.py": 200}}, None),
    )
    # No ledger entry at all — irrelevant for a lowering.
    mocker.patch("app.size_limit_policy.parse_bump_history", return_value=[])
    assert _run(jp, repo, "--sections", "backend") == 0


def test_check_cap_raise_guard_fails_closed_on_unreadable_history(
    tmp_path, capsys, mocker
):
    """A raise whose justification cannot be read fails CLOSED (flagged), never
    degraded to 'no bump on record' — the chronic-offender classifier's contract."""
    from app.size_limit_policy import BumpHistoryUnavailableError

    repo, jp = _build_repo(tmp_path, backend={"m.py": (50, 200)})
    mocker.patch(
        "testing.check_size_limits._mainline_limits_via_git",
        return_value=({"backend": {"m.py": 100}}, None),
    )
    mocker.patch(
        "app.size_limit_policy.parse_bump_history",
        side_effect=BumpHistoryUnavailableError("ledger gone"),
    )
    rc = _run(jp, repo, "--sections", "backend")
    assert rc == 1
    assert "[cap-raise] backend/m.py: cap raised 100 -> 200" in capsys.readouterr().out


def test_check_cap_raise_guard_skips_when_mainline_unavailable(
    tmp_path, capsys, mocker
):
    """A shallow clone / checkout without the mainline ancestry must SKIP the
    raise guard with a clear message — not crash and not spuriously fail."""
    repo, jp = _build_repo(tmp_path, backend={"m.py": (50, 200)})
    mocker.patch(
        "testing.check_size_limits._mainline_limits_via_git",
        return_value=(None, "no mainline branch present (ref helper fell back to HEAD)"),
    )
    rc = _run(jp, repo, "--sections", "backend")
    assert rc == 0
    assert "[cap-raise-guard] skipped" in capsys.readouterr().out


def test_check_cap_raise_guard_skips_non_git_checkout(tmp_path, capsys):
    """A non-git repo root (hermetic fixture) cannot establish a mainline
    baseline — the guard degrades to a clear skip, not a failure."""
    repo, jp = _build_repo(tmp_path, backend={"m.py": (50, 200)})
    assert _run(jp, repo, "--sections", "backend") == 0
    assert "[cap-raise-guard] skipped" in capsys.readouterr().out


def test_check_flags_phantom_ledger_records(tmp_path, capsys, mocker):
    """CHECK must FAIL when the cap-bump-history ledger holds a record the
    mainline git history cannot justify (a phantom) — naming the file, the old
    and new values, the commit, and the remedy. This is the reverse direction
    of the ledger↔git contract (ledger ⊆ git) and it must NOT be skipped on a
    truncated checkout."""
    repo, jp = _build_repo(tmp_path, backend={"m.py": (50, 100)})
    # Simulate a phantom: ledger claims 50 -> 100 at a commit whose diff shows
    # no such bump.
    mocker.patch(
        "app.size_limit_policy.phantom_ledger_records",
        return_value=[
            ("backend", "m.py", 50, 100, "deadbeefcafe00000000000000000000000000",
             "commit's diff shows no recorded bump for this key/pair")
        ],
    )
    rc = _run(jp, repo, "--sections", "backend")
    assert rc == 1
    out = capsys.readouterr().out
    assert "[ledger-phantom] backend/m.py: ledger records 50 -> 100" in out
    assert "cap_bump_history.json" in out
    assert "--fix" in out


def test_check_passes_with_clean_ledger(tmp_path, capsys, mocker):
    """CHECK passes when the ledger holds no phantom records — a clean mirror is
    not a failure even when every other guard is green."""
    repo, jp = _build_repo(tmp_path, backend={"m.py": (50, 100)})
    mocker.patch("app.size_limit_policy.phantom_ledger_records", return_value=[])
    assert _run(jp, repo, "--sections", "backend") == 0
    out = capsys.readouterr().out
    assert "[ledger-phantom]" not in out


def test_fix_prunes_phantom_ledger_records(tmp_path, capsys, mocker):
    """--fix self-heals a phantom ledger even when no cap breach exists to bump:
    the checker must rebuild the ledger (pruning phantoms) rather than report
    'no breaches to fix' and leave the phantom in place."""
    repo, jp = _build_repo(tmp_path, backend={"m.py": (50, 100)})
    mocker.patch(
        "app.size_limit_policy.phantom_ledger_records",
        return_value=[
            ("backend", "m.py", 50, 100, "deadbeefcafe00000000000000000000000000",
             "commit's diff shows no recorded bump for this key/pair")
        ],
    )
    rebuilt = mocker.patch("app.size_limit_policy.rebuild_cap_bump_history_ledger")
    assert _run(jp, repo, "--fix", "--sections", "backend") == 0
    out = capsys.readouterr().out
    assert "Pruned 1 phantom record(s)" in out
    rebuilt.assert_called_once()
    # The rebuild is invoked with the bumps applied by this fix (none here) so
    # pruning is the only operation.
    assert rebuilt.call_args.kwargs["pending"] == []


def _git(repo_root, *args):
    subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _write_git_repo(repo_root, caps):
    """Scaffold ``backend/testing/module_size_limits.json`` at ``repo_root`` with
    ``caps`` and commit it as the initial commit on the given branch."""
    (repo_root / "backend" / "testing").mkdir(parents=True)
    (repo_root / "backend" / "app").mkdir(parents=True)
    limits = dict(caps)
    limits["_doc"] = "test doc"
    with open(repo_root / "backend" / "testing" / "module_size_limits.json", "w") as f:
        json.dump(limits, f)
    _git(repo_root, "init", "-q", "-b", "main")
    _git(repo_root, "config", "user.email", "test@example.com")
    _git(repo_root, "config", "user.name", "Test")
    _git(repo_root, "add", ".")
    _git(repo_root, "commit", "-q", "-m", "initial")


def test_mainline_limits_via_git_reads_mainline_caps(tmp_path):
    """_mainline_limits_via_git reads module_size_limits.json AT the mainline ref,
    not the working tree — so a local raise is still judged against the committed
    ancestry baseline."""
    repo = tmp_path / "repo"
    _write_git_repo(repo, {"backend": {"m.py": 100}})
    # The working tree now raises the cap; the committed mainline ref still has 100.
    limits = json.loads(
        (repo / "backend" / "testing" / "module_size_limits.json").read_text()
    )
    limits["backend"]["m.py"] = 200
    (repo / "backend" / "testing" / "module_size_limits.json").write_text(
        json.dumps(limits)
    )
    baseline, reason = csl._mainline_limits_via_git(str(repo))
    assert reason is None
    assert baseline["backend"]["m.py"] == 100


def test_mainline_limits_via_git_skips_when_mainline_ref_absent(tmp_path):
    """A checkout with no mainline branch (only a feature branch — the shape of a
    shallow CI clone that never fetched the integration branch) degrades to a
    clear skip, not a crash or a spurious failure."""
    repo = tmp_path / "repo"
    (repo / "backend" / "testing").mkdir(parents=True)
    (repo / "backend" / "testing" / "module_size_limits.json").write_text(
        json.dumps({"backend": {"m.py": 100}})
    )
    _git(repo, "init", "-q", "-b", "feature/size-work")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feature work")
    baseline, reason = csl._mainline_limits_via_git(str(repo))
    assert baseline is None
    assert "no mainline branch present" in reason


def test_fix_records_bump_then_check_accepts_raise(tmp_path, capsys, mocker):
    """End-to-end: --fix bumps a cap AND records the (old -> new) pair in the
    fixture cap_bump_history.json; a follow-up CHECK accepts the raise because
    the ledger now justifies it."""
    repo, jp = _build_repo(tmp_path, backend={"big.py": (1720, 1600)})
    mocker.patch("app.size_limit_policy.parse_bump_history", return_value=[])
    assert _run(jp, repo, "--fix", "--sections", "backend") == 0

    new_cap = bump_size_limit(1720)
    ledger_path = os.path.join(os.path.dirname(jp), "cap_bump_history.json")
    ledger = json.loads(_read(ledger_path))
    assert (1600, new_cap) in [
        (r["old_cap"], r["new_cap"]) for r in ledger["history"]["backend"]["big.py"]
    ]

    # Undo the empty-history mock (used only so --fix treats big.py as a
    # first-time breach) before the follow-up check reads the real ledger.
    mocker.stopall()
    # Follow-up CHECK: the mainline baseline holds the pre-fix cap, so the bump
    # is a raise vs the ancestry — but the freshly-recorded ledger entry makes it
    # deliberate and legal. Pin the ledger branch (deployed-layout read) so the
    # test is deterministic regardless of whether _REPO_ROOT is a complete
    # checkout (where parse_bump_history would otherwise read git, not the
    # fixture mirror).
    slp._bump_history_cache.clear()
    mocker.patch("app.size_limit_policy._git_reads_history_here", return_value=False)
    mocker.patch(
        "testing.check_size_limits._mainline_limits_via_git",
        return_value=({"backend": {"big.py": 1600}}, None),
    )
    mocker.patch("app.size_limit_policy._CAP_BUMP_HISTORY_LEDGER_PATH", ledger_path)
    assert _run(jp, repo, "--sections", "backend") == 0
