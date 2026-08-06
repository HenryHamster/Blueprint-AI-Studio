"""Tests for the standalone size-guardrail checker (check_size_limits.py).

Covers: check mode flags a real breach (non-zero exit); check mode passes when
clean; --fix bumps a breached cap to exactly bump_size_limit(current) and leaves
other entries untouched; the rewritten JSON preserves structure and re-checks
clean; section scoping works; absent-tree sections are skipped not errored.
"""

import json
import os
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


def test_fix_chronic_git_unavailable_refuses_bump(tmp_path, capsys, mocker):
    """If git is unavailable (subprocess error), --fix must FAIL CLOSED — skip
    the cap bump and route to decomposition, not degrade to a routine bump.

    Unreadable bump history is NOT the same as "no bumps on record"; a possibly
    chronic offender must not receive a silent routine bump when its history
    cannot be read (e.g. a truncated task-VM clone).
    """
    # Distinct key from the other real-subprocess tests so the module TTL cache
    # cannot leak a genuinely-empty history into this unreadable-history case.
    slp._bump_history_cache.clear()
    repo, jp = _build_repo(tmp_path, backend={"big_unreadable.py": (1720, 1600)})

    # Simulate git subprocess failing entirely (e.g., non-repo CI shard).
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
