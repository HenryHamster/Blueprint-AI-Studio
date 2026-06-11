#!/usr/bin/env python3
"""Build the before/after manifest for scripts/viewport-snapshot.sh.

Invoked by reference from the bash script (per the CLAUDE.md cross-language
codegen rule — a real, lintable helper file rather than an embedded heredoc):

    python3 scripts/viewport_manifest.py <before_dir> <after_dir> \
        <base> <run_id> <out_manifest_path>

It pairs every captured PNG under <after_dir> with its counterpart under
<before_dir> (same relative path: <fileSlug>/<nameSlug>-<width>px.png), derives
a per-pair status from a byte comparison, writes the JSON manifest, and prints a
changed-first table plus the `Read` paths for changed/new pairs to stdout.

Pair status:
  - changed   — present on both sides, bytes differ
  - unchanged — present on both sides, bytes identical
  - new       — present only in after (test absent on base, or new component)
  - removed   — present only in before (test/component removed on this branch)

Exit code is always 0: snapshotting is informational and never a gate.
"""
from __future__ import annotations

import filecmp
import json
import os
import re
import sys

_WIDTH_RE = re.compile(r"-(\d+)px\.png$")
# Status sort order for the printed table: changed first, then new, removed, unchanged.
_STATUS_ORDER = {"changed": 0, "new": 1, "removed": 2, "unchanged": 3}


def _collect(root: str) -> dict[str, str]:
    """Map relative PNG path -> absolute path for every *.png under root."""
    found: dict[str, str] = {}
    if not os.path.isdir(root):
        return found
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(".png"):
                continue
            abspath = os.path.join(dirpath, name)
            rel = os.path.relpath(abspath, root)
            found[rel] = abspath
    return found


def _unslug_test_file(file_slug: str) -> str:
    """Best-effort reverse of _slug() on the viewport-test filename.

    The capture hook slugifies the caller file (e.g. `TaskCard.viewport.test.tsx`
    -> `taskcard-viewport-test-tsx`). Casing is not recoverable, but restoring the
    `.viewport.test.tsx` suffix keeps the manifest readable.
    """
    suffix = "-viewport-test-tsx"
    if file_slug.endswith(suffix):
        return file_slug[: -len(suffix)] + ".viewport.test.tsx"
    return file_slug


def _width_of(rel: str) -> int | None:
    m = _WIDTH_RE.search(rel)
    return int(m.group(1)) if m else None


def _test_name(rel: str) -> str:
    base = os.path.basename(rel)
    return _WIDTH_RE.sub("", base)


def build(before_dir: str, after_dir: str, base: str, run_id: str, out_path: str) -> None:
    before = _collect(before_dir)
    after = _collect(after_dir)

    pairs: list[dict[str, object]] = []
    for rel in sorted(set(before) | set(after)):
        b = before.get(rel)
        a = after.get(rel)
        if a and b:
            status = "unchanged" if filecmp.cmp(b, a, shallow=False) else "changed"
        elif a:
            status = "new"
        else:
            status = "removed"
        file_slug = rel.split(os.sep, 1)[0]
        pairs.append(
            {
                "test_file": _unslug_test_file(file_slug),
                "test": _test_name(rel),
                "width": _width_of(rel),
                "before": b,
                "after": a,
                "status": status,
            }
        )

    summary = {"changed": 0, "unchanged": 0, "new": 0, "removed": 0}
    for p in pairs:
        summary[str(p["status"])] += 1

    manifest = {"base": base, "run_id": run_id, "pairs": pairs, "summary": summary}
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")

    _print_report(pairs, summary, out_path)


def _print_report(
    pairs: list[dict[str, object]], summary: dict[str, int], out_path: str
) -> None:
    print(f"\nViewport snapshot manifest: {out_path}")
    print(
        "Summary: "
        f"{summary['changed']} changed, {summary['new']} new, "
        f"{summary['removed']} removed, {summary['unchanged']} unchanged"
    )
    if not pairs:
        print("(no PNG pairs captured)")
        return

    ordered = sorted(
        pairs, key=lambda p: (_STATUS_ORDER.get(str(p["status"]), 9), str(p["test_file"]), str(p["test"]))
    )
    print("\n  STATUS     WIDTH  TEST FILE / TEST")
    print("  " + "-" * 60)
    for p in ordered:
        width = p["width"] if p["width"] is not None else "?"
        print(f"  {str(p['status']):<10} {str(width):>5}  {p['test_file']} :: {p['test']}")

    # Read paths for the pairs worth inspecting (changed + new).
    inspect = [p for p in ordered if p["status"] in ("changed", "new")]
    if inspect:
        print("\nRead the before/after PNGs for these pairs:")
        for p in inspect:
            print(f"  [{p['status']}] {p['test_file']} :: {p['test']}")
            if p["before"]:
                print(f"    before: {p['before']}")
            if p["after"]:
                print(f"    after:  {p['after']}")
    else:
        print("\nNo changed or new pairs to inspect.")


def main(argv: list[str]) -> int:
    if len(argv) != 6:
        print(
            "usage: viewport_manifest.py <before_dir> <after_dir> <base> <run_id> <out_manifest>",
            file=sys.stderr,
        )
        return 2
    _script, before_dir, after_dir, base, run_id, out_path = argv
    build(before_dir, after_dir, base, run_id, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
