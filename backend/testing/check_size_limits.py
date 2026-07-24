#!/usr/bin/env python3
"""Fast, dependency-free module size-guardrail checker.

Mirrors the line-count guardrail enforced by ``backend/testing/test_invariants.py``
(``module_size_limits.json``) so an agent can surface and remediate size breaches
*before* opening a PR — without spinning up Docker, a DB, or the full test suite.

Two modes:

* check mode (default): line-count every file listed in the scoped section(s) and
  print each breach (path, current lines, cap). Exits non-zero if any file exceeds
  its cap, zero when clean.
* ``--fix`` mode: for every breached file, rewrite *only* that file's cap in the
  tracked JSON to the canonical ``bump_size_limit`` policy value; AND for every
  uncapped source file over ``_COVERAGE_THRESHOLD`` lines (the complementary
  coverage guardrail the ``test_*_no_uncapped_large_file`` meta-tests enforce),
  ADD a new cap entry sized at the same policy headroom. Both paths preserve key
  order and the leading ``_doc`` field so the diff stays minimal. After ``--fix``
  re-running check mode passes. The bump stays policy-consistent so the guardrail
  still catches genuine runaway growth.

Usage (from the repo root, no install step)::

    python3 backend/testing/check_size_limits.py                 # check all sections
    python3 backend/testing/check_size_limits.py --sections backend
    python3 backend/testing/check_size_limits.py --fix           # bump breached caps
    python3 backend/testing/check_size_limits.py --base-root /tmp/base_wt
                                  # baseline-aware: block only NEW/GROWN offenders

Stdlib only. The three sections resolve their source roots exactly the way
``test_invariants.py`` does: backend -> ``backend/app``, agent -> ``agent/src``,
frontend -> ``admin-ui/src`` (honoring the ``ADMIN_UI_SRC`` override and the
Docker ``/admin-ui/src`` mount). A section whose source tree is absent in the
current environment is reported as skipped rather than treated as an error.
"""

import argparse
import json
import os
import sys

# Make ``app.size_limit_policy`` importable when run as
# ``python3 backend/testing/check_size_limits.py`` from the repo root: add the
# backend dir (this file's parent's parent) to sys.path. The policy itself is
# imported lazily (in ``_bump_size_limit``) so this path setup runs first.
_BACKEND_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


def _bump_size_limit(current_size: int) -> int:
    """Canonical bump policy, shared with the server endpoint.

    Imported lazily so the ``sys.path`` setup above runs before the import and
    the module stays runnable as a bare script from the repo root."""
    from app.size_limit_policy import bump_size_limit

    return bump_size_limit(current_size)


def _classify_breach(
    module_key: str,
    current_size: int,
    section: str = "backend",
    *,
    override_chronic: bool = False,
):
    """Chronic-offender-aware breach verdict, shared with the server endpoint.

    Imported lazily for the same reason as ``_bump_size_limit`` above."""
    from app.size_limit_policy import classify_breach

    return classify_breach(
        module_key, current_size, section, override_chronic=override_chronic
    )


def classify_size_record(
    rel: str,
    count: "int | None",
    cap: "int | None",
    section: str = "backend",
    *,
    override_chronic: bool = False,
    with_verdict: bool = True,
) -> dict:
    """Content-injectable breach-and-bump classification for ONE counted record.

    Pure / filesystem-free: the caller supplies the already-computed line
    ``count`` (``None`` when the file is absent / unreadable) and the ``cap``.
    This is the SINGLE source of truth for the breach test (``count > cap``) and
    the chronic/bump policy (``_classify_breach``), so the disk-mode CLI path
    (via :func:`scan_section`) and the at-SHA evaluator
    (``backend/app/ci_compliance.py``) classify identically without duplicating
    the policy.

    Returns ``{rel, count, cap, breach, verdict}``:

      * ``breach`` — True only when BOTH ``count`` and ``cap`` are present and
        ``count > cap``. A file absent at the ref (``count is None``) is never a
        breach (a missing file cannot have grown past its cap).
      * ``verdict`` — the ``BreachVerdict`` from :func:`_classify_breach` when
        ``breach`` (and ``with_verdict``), else ``None``. ``with_verdict=False``
        skips the (git-touching) chronic lookup for the plain check path, which
        only needs the breach flag.
    """
    breach = count is not None and cap is not None and count > cap
    verdict = None
    if breach and with_verdict:
        verdict = _classify_breach(
            rel, count, section, override_chronic=override_chronic
        )
    return {"rel": rel, "count": count, "cap": cap, "breach": breach, "verdict": verdict}


# Repo root is two levels up from this file (backend/testing -> repo root).
_DEFAULT_REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
_DEFAULT_JSON_PATH = os.path.join(os.path.dirname(__file__), "module_size_limits.json")

# Section order is significant: matches the JSON's section order so output and
# --sections=all iteration are deterministic.
SECTIONS = ("backend", "agent", "frontend", "testing")

# Coverage threshold for the complementary guardrail: a source file larger than
# this with NO cap entry at all is invisible to the per-file caps and can grow
# without limit. Mirrors ``test_invariants._COVERAGE_THRESHOLD`` (and the agent /
# frontend coverage meta-tests). Kept in sync by a guard test in
# test_size_limits_checker.py. NEVER mirror this by importing test_invariants —
# that module pulls in pytest/app machinery and would break the stdlib-only,
# dependency-free contract this checker guarantees.
_COVERAGE_THRESHOLD = 1500


def _is_frontend_excluded(rel_path: str) -> bool:
    """Mirror test_invariants._is_frontend_excluded: skip frontend test/type files."""
    return rel_path.endswith((".test.ts", ".test.tsx", ".d.ts")) or rel_path.startswith(
        "test/"
    )


def _is_agent_excluded(rel_path: str) -> bool:
    """Mirror test_invariants._is_agent_excluded: skip agent test/type files."""
    return rel_path.endswith((".test.ts", ".d.ts"))


def _no_exclusion(_rel_path: str) -> bool:
    return False


# Per-section scan rules for the uncapped-file coverage guardrail. Mirrors the
# suffix tuples and exclusion predicates the coverage meta-tests in
# test_invariants.py use, so --fix adds the exact caps those tests demand.
_SECTION_SCAN = {
    "backend": ((".py",), _no_exclusion),
    "agent": ((".ts",), _is_agent_excluded),
    "frontend": ((".ts", ".tsx"), _is_frontend_excluded),
    "testing": ((".py",), _no_exclusion),
}


def resolve_section_root(
    section: str, repo_root: str, *, use_env_overrides: bool = True
) -> str | None:
    """Resolve a section's source tree root, mirroring test_invariants.py.

    Returns the directory path if it exists, else None (caller skips the
    section). frontend honors the ``ADMIN_UI_SRC`` override and the Docker
    ``/admin-ui/src`` mount before falling back to ``<repo>/admin-ui/src``.

    ``use_env_overrides`` gates the environment-specific frontend escape
    hatches (the ``ADMIN_UI_SRC`` env var and the global ``/admin-ui/src``
    Docker mount). They make sense only for the *real* run rooted at the
    default repo root; when an explicit ``--repo-root`` is supplied (e.g. a
    hermetic test fixture) resolution must stay strictly under that root so a
    stray global mount does not leak the host environment into the result.
    """
    if section == "backend":
        root = os.path.join(repo_root, "backend", "app")
        return root if os.path.isdir(root) else None
    if section == "agent":
        root = os.path.join(repo_root, "agent", "src")
        return root if os.path.isdir(root) else None
    if section == "frontend":
        candidates = []
        if use_env_overrides:
            env_override = os.getenv("ADMIN_UI_SRC")
            if env_override:
                candidates.append(env_override)
            candidates.append("/admin-ui/src")
        candidates.append(os.path.join(repo_root, "admin-ui", "src"))
        for candidate in candidates:
            if os.path.isdir(candidate):
                return candidate
        return None
    if section == "testing":
        root = os.path.join(repo_root, "backend", "testing")
        return root if os.path.isdir(root) else None
    raise ValueError(f"Unknown section: {section}")


def count_lines(path: str) -> int:
    """Line-count a file the same way test_invariants.py does.

    The line count is encoding-invariant (only the newline byte matters), so
    ``errors="replace"`` yields the identical count to test_invariants.py's
    default ``open()`` for our utf-8 sources. ``errors="replace"`` (not a bare
    utf-8 pin) is deliberate: this checker runs on EVERY ./check now, so a stray
    non-utf-8 file must NOT raise UnicodeDecodeError and hard-fail every agent's
    gate fleet-wide — it degrades (still counts lines correctly) and any real
    encoding problem still surfaces via the actual test suite.
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        return len(f.readlines())


def scan_section(caps: dict, root: str, section: str = "backend") -> list[dict]:
    """Line-count every file in ``caps`` under ``root``.

    Returns one record per file: ``{rel, path, count, cap, breach, verdict}``.
    A file missing from disk is recorded with ``count=None`` and
    ``breach=False`` (a missing file can't have grown past its cap).

    The breach test is delegated to :func:`classify_size_record` (the shared,
    filesystem-free policy) so the disk-mode CLI and the at-SHA evaluator agree
    bit-for-bit. ``with_verdict=False`` keeps the plain check path free of the
    per-file git lookup the chronic verdict would trigger.
    """
    records = []
    for rel in caps:
        cap = caps[rel]
        path = os.path.join(root, rel)
        count = count_lines(path) if os.path.isfile(path) else None
        rec = classify_size_record(rel, count, cap, section, with_verdict=False)
        rec["path"] = path
        records.append(rec)
    return records


def scan_uncapped_section(
    root: str, caps: dict, suffixes: tuple, is_excluded
) -> list[dict]:
    """Walk ``root`` for source files over ``_COVERAGE_THRESHOLD`` lines that have
    NO cap entry in ``caps``.

    Mirrors ``test_invariants._scan_uncapped``: collects every file whose name
    ends in one of ``suffixes``, is not skipped by ``is_excluded``, exceeds the
    coverage threshold, and is absent from ``caps``. Returns one record per file
    (``{rel, path, count}``) sorted largest-first. Tree-relative paths use forward
    slashes to match the JSON key convention.
    """
    offenders = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(suffixes):
                continue
            abs_path = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            if is_excluded(rel) or rel in caps:
                continue
            count = count_lines(abs_path)
            if count > _COVERAGE_THRESHOLD:
                offenders.append({"rel": rel, "path": abs_path, "count": count})
    offenders.sort(key=lambda rec: rec["count"], reverse=True)
    return offenders


def load_limits(json_path: str) -> dict:
    """Load the size-limits JSON, preserving key order (dicts are ordered)."""
    with open(json_path) as f:
        return json.load(f)


def write_limits(json_path: str, data: dict) -> None:
    """Write the size-limits JSON back, preserving structure and key order.

    Uses the same formatting as the tracked file (2-space indent, ASCII-escaped,
    trailing newline) so a --fix diff shows only the changed cap numbers.
    """
    with open(json_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=True)
        f.write("\n")


def evaluate(
    data: dict, sections: list[str], repo_root: str, *, use_env_overrides: bool = True
) -> tuple[dict[str, list[dict]], list[str]]:
    """Evaluate the scoped sections.

    Returns ``(checked, skipped)`` where ``checked`` maps section -> list of
    file records (from scan_section) and ``skipped`` is the list of section
    names whose source tree was absent. ``use_env_overrides`` is forwarded to
    ``resolve_section_root`` (see its docstring).
    """
    checked: dict[str, list[dict]] = {}
    skipped: list[str] = []
    for section in sections:
        caps = data.get(section) or {}
        root = resolve_section_root(
            section, repo_root, use_env_overrides=use_env_overrides
        )
        if root is None:
            skipped.append(section)
            continue
        checked[section] = scan_section(caps, root, section)
    return checked, skipped


def _collect_baseline_offenders(
    base_root: str, sections: list[str]
) -> "tuple[dict, dict, str | None]":
    """Offender maps for a BASE-branch checkout, or why the baseline is unusable.

    Returns ``(breached, uncapped, unavailable_reason)``. ``breached`` maps
    ``(section, rel) -> line count`` for every file already over its cap on the
    base checkout — judged against the BASE's own ``module_size_limits.json``
    (the caps this change inherited), not the PR's. ``uncapped`` maps
    ``(section, rel) -> line count`` for every base source file over the
    coverage threshold with no cap entry there. When the base checkout has no
    readable limits JSON the baseline cannot be computed: both maps come back
    empty with a reason, so the caller fails CLOSED (every PR-side offender is
    treated as newly introduced).

    Resolution is hermetic (``use_env_overrides=False``): the ADMIN_UI_SRC /
    ``/admin-ui/src`` escape hatches would leak the PR tree into the baseline.
    """
    base_json = os.path.join(base_root, "backend", "testing", "module_size_limits.json")
    try:
        base_data = load_limits(base_json)
    except (OSError, ValueError) as exc:
        return {}, {}, f"cannot read {base_json}: {exc}"
    checked, _skipped = evaluate(base_data, sections, base_root, use_env_overrides=False)
    breached = {
        (section, rec["rel"]): rec["count"]
        for section, recs in checked.items()
        for rec in recs
        if rec["breach"]
    }
    uncapped: dict = {}
    for section in sections:
        root = resolve_section_root(section, base_root, use_env_overrides=False)
        if root is None:
            continue
        suffixes, is_excluded = _SECTION_SCAN[section]
        caps = base_data.get(section) or {}
        for rec in scan_uncapped_section(root, caps, suffixes, is_excluded):
            uncapped[(section, rec["rel"])] = rec["count"]
    return breached, uncapped, None


def _split_preexisting(
    offenders: "list[tuple[str, dict]]", base_counts: dict
) -> "tuple[list, list]":
    """Partition offenders into (blocking, preexisting) against baseline counts.

    An offender is PRE-EXISTING — inherited from the base branch, not this
    change's responsibility — only when the same file was already an offender of
    the same class on base AND has not grown past its base line count here. A
    file that is clean on base, or that GREW while over-cap, is BLOCKING.

    Entries come back as ``(section, rec, base_count)`` — ``base_count`` is
    ``None`` for an offender absent from the baseline (including the
    no-baseline case, where ``base_counts`` is empty and everything blocks).
    """
    blocking: list = []
    preexisting: list = []
    for section, rec in offenders:
        base_count = base_counts.get((section, rec["rel"]))
        if base_count is not None and rec["count"] <= base_count:
            preexisting.append((section, rec, base_count))
        else:
            blocking.append((section, rec, base_count))
    return blocking, preexisting


def _parse_sections(raw: str) -> list[str]:
    if raw.strip().lower() == "all":
        return list(SECTIONS)
    requested = [s.strip() for s in raw.split(",") if s.strip()]
    invalid = [s for s in requested if s not in SECTIONS]
    if invalid:
        raise argparse.ArgumentTypeError(
            f"unknown section(s): {', '.join(invalid)}; choose from {', '.join(SECTIONS)}"
        )
    # Preserve canonical order regardless of how the user ordered them.
    return [s for s in SECTIONS if s in requested]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check (and optionally --fix) module size-limit guardrails."
    )
    parser.add_argument(
        "--sections",
        type=_parse_sections,
        default=list(SECTIONS),
        help="comma-separated subset of {backend,agent,frontend} (default: all)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="bump the cap of each breached file to the canonical policy value",
    )
    parser.add_argument(
        "--force-chronic",
        action="store_true",
        dest="force_chronic",
        help=(
            "operator-approval override: allow --fix to bump a chronic offender's cap. "
            "Attribution is recorded in the output. Without this flag, chronic offenders "
            "are skipped and exit 1 is returned."
        ),
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default=_DEFAULT_JSON_PATH,
        help="path to module_size_limits.json (default: alongside this script)",
    )
    parser.add_argument(
        "--repo-root",
        dest="repo_root",
        default=None,
        help="repo root used to resolve section source trees (default: inferred)",
    )
    parser.add_argument(
        "--base-root",
        dest="base_root",
        default=None,
        help=(
            "path to a checkout of the BASE branch (e.g. a detached worktree of "
            "origin/automain). Check mode then blocks only on offenders "
            "introduced or grown by this change; an offender already present at "
            "the same-or-larger size on base (judged against the base's own "
            "module_size_limits.json) is reported as [pre-existing] and does "
            "not affect the exit code. Ignored under --fix."
        ),
    )
    args = parser.parse_args(argv)
    repo_root_explicit = args.repo_root is not None
    if args.repo_root is None:
        args.repo_root = _DEFAULT_REPO_ROOT

    data = load_limits(args.json_path)
    # The environment-specific frontend escape hatches (ADMIN_UI_SRC /
    # /admin-ui/src) apply only to the real run when the caller did not pass an
    # explicit --repo-root. An explicit root means hermetic resolution under
    # that checkout, even if it happens to equal the default path.
    use_env_overrides = not repo_root_explicit
    checked, skipped = evaluate(
        data, args.sections, args.repo_root, use_env_overrides=use_env_overrides
    )

    for section in skipped:
        print(f"[skip] {section}: source tree not present in this environment")

    breaches = [
        (section, rec)
        for section in args.sections
        for rec in checked.get(section, [])
        if rec["breach"]
    ]

    # Complementary guardrail: uncapped source files over the coverage threshold.
    # A capped-but-breached file is handled by ``breaches`` above; a file with no
    # cap at all is invisible to that scan, so walk each scoped section's tree.
    uncapped: list[tuple[str, dict]] = []
    for section in args.sections:
        if section in skipped:
            continue
        # Keep the uncapped scan on the same root resolution path as evaluate().
        root = resolve_section_root(
            section, args.repo_root, use_env_overrides=use_env_overrides
        )
        if root is None:
            continue
        suffixes, is_excluded = _SECTION_SCAN[section]
        caps = data.get(section) or {}
        for rec in scan_uncapped_section(root, caps, suffixes, is_excluded):
            uncapped.append((section, rec))

    if args.fix:
        if not breaches and not uncapped:
            print("No breaches to fix — all scoped files within cap.")
            return 0
        chronic_skipped: list[tuple[str, dict, str]] = []
        bumped: list[tuple[str, dict, int]] = []
        added: list[tuple[str, dict, int]] = []
        for section, rec in breaches:
            verdict = classify_size_record(
                rec["rel"],
                rec["count"],
                rec["cap"],
                section,
                override_chronic=args.force_chronic,
            )["verdict"]
            if verdict["is_chronic"] and not args.force_chronic:
                print(
                    f"[chronic] {section}/{rec['rel']}: {rec['count']} lines — "
                    f"{verdict['reason']} Skipping cap bump; use --force-chronic "
                    f"to override with operator approval."
                )
                chronic_skipped.append((section, rec, verdict["reason"]))
            else:
                new_cap = verdict["suggested_new_limit"]
                assert new_cap is not None
                data[section][rec["rel"]] = new_cap
                label = "[fix-chronic]" if verdict["is_chronic"] else "[fix]"
                print(
                    f"{label}  {section}/{rec['rel']}: {rec['count']} lines — "
                    f"cap {rec['cap']} -> {new_cap}"
                    + (
                        f" (operator-approved; {verdict['bump_count']} prior bumps)"
                        if verdict["is_chronic"]
                        else ""
                    )
                )
                bumped.append((section, rec, new_cap))
        for section, rec in uncapped:
            # Uncapped files have no bump history to check — they are new to the
            # tracking system, so they cannot be chronic offenders yet.
            new_cap = _bump_size_limit(rec["count"])
            data.setdefault(section, {})[rec["rel"]] = new_cap
            print(
                f"[add]  {section}/{rec['rel']}: {rec['count']} lines — "
                f"added cap {new_cap}"
            )
            added.append((section, rec, new_cap))
        if bumped or added:
            write_limits(args.json_path, data)
            actions = []
            if bumped:
                actions.append(f"Bumped {len(bumped)} cap(s)")
            if added:
                actions.append(f"Added {len(added)} cap(s)")
            print(f"{'; '.join(actions)} in {args.json_path}.")
        if chronic_skipped:
            print(
                f"\n{len(chronic_skipped)} chronic offender(s) skipped — "
                "decomposition required before bumping cap. "
                "Use --force-chronic with operator approval to override."
            )
            return 1
        return 0

    # Baseline comparison: with --base-root, only offenders INTRODUCED or GROWN
    # by this change block; offenders inherited from the base branch at the same
    # or larger size there are reported as [pre-existing] and never fail the
    # check — an upstream breach (e.g. a merge that pushed a chronic offender
    # over its cap on automain) must not block every unrelated PR. An unreadable
    # baseline fails CLOSED: everything is treated as newly introduced.
    base_breached: dict = {}
    base_uncapped: dict = {}
    if args.base_root:
        base_breached, base_uncapped, baseline_reason = _collect_baseline_offenders(
            args.base_root, args.sections
        )
        if baseline_reason:
            print(
                f"[baseline] unavailable ({baseline_reason}) — treating every "
                "offender as introduced by this change"
            )

    exit_code = 0
    blocking_breaches, preexisting_breaches = _split_preexisting(
        breaches, base_breached
    )
    if blocking_breaches:
        for section, rec, base_count in blocking_breaches:
            grew = f"; grew from {base_count} lines on base" if base_count is not None else ""
            print(
                f"[breach] {section}/{rec['rel']}: {rec['count']} lines "
                f"(cap: {rec['cap']}){grew}"
            )
        print(
            f"\n{len(blocking_breaches)} size breach(es). Run with --fix to bump "
            f"the breached caps to the canonical policy value."
        )
        exit_code = 1

    blocking_uncapped, preexisting_uncapped = _split_preexisting(
        uncapped, base_uncapped
    )
    if blocking_uncapped:
        for section, rec, base_count in blocking_uncapped:
            grew = f"; grew from {base_count} lines on base" if base_count is not None else ""
            print(
                f"[uncapped] {section}/{rec['rel']}: {rec['count']} lines "
                f"(threshold: {_COVERAGE_THRESHOLD}) — NO cap entry{grew}"
            )
        print(
            f"\n{len(blocking_uncapped)} uncapped file(s) over "
            f"{_COVERAGE_THRESHOLD} lines violate the size guardrail (no cap "
            f"entry). Run with --fix to add a cap entry (current size + policy "
            f"headroom)."
        )
        exit_code = 1

    preexisting = [
        ("cap breach", section, rec, base_count)
        for section, rec, base_count in preexisting_breaches
    ] + [
        ("uncapped", section, rec, base_count)
        for section, rec, base_count in preexisting_uncapped
    ]
    if preexisting:
        for kind, section, rec, base_count in preexisting:
            print(
                f"[pre-existing] {section}/{rec['rel']}: {rec['count']} lines "
                f"({kind}; already {base_count} lines on base) — inherited from "
                f"the base branch, not introduced by this change"
            )
        print(
            f"\n{len(preexisting)} pre-existing offender(s) inherited from the "
            f"base branch — NOT blocking this change. Upstream remediation "
            f"needed: a dedicated chore PR running check_size_limits.py --fix "
            f"(chronic offenders require operator approval via --force-chronic)."
        )

    if exit_code == 0 and not preexisting:
        print("All scoped files within their size caps.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
