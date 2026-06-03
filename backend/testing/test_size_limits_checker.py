"""Tests for the standalone size-guardrail checker (check_size_limits.py).

Covers: check mode flags a real breach (non-zero exit); check mode passes when
clean; --fix bumps a breached cap to exactly bump_size_limit(current) and leaves
other entries untouched; the rewritten JSON preserves structure and re-checks
clean; section scoping works; absent-tree sections are skipped not errored.
"""

import json
import os

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
