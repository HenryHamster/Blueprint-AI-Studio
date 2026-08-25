#!/usr/bin/env python3
"""Custom git merge driver for ``backend/testing/module_size_limits.json``.

This one JSON file is the single most-frequently-edited file in the repo: every
PR that legitimately grows a module bumps that module's line-count cap here. Two
in-flight PRs that each bump a cap therefore routinely produce a textual git
conflict on this file even though the *intent* never conflicts — both sides only
ever raise caps, and policy NEVER ratchets a cap down (see the ``_doc`` field).

So a max-cap union merge is provably safe: for every key present on either side
take the LARGER cap, keep keys that exist on only one side, and never lower a cap
or drop a key. The result is byte-stable and matches ``check_size_limits.write_limits``
(2-space indent, ASCII-escaped, trailing newline) so the resolved file is the same
bytes regardless of merge order.

Registered as the ``sizecap`` driver via ``.gitattributes`` + a ``merge.sizecap.driver``
git-config entry (the agent's workspace-prep step installs the config — a clone does
NOT carry it). Git invokes it as::

    python3 backend/testing/merge_size_limits.py %O %A %B

where %O is the common-ancestor blob, %A is OUR version (also the OUTPUT path the
driver must overwrite), and %B is THEIR version. Exit 0 → conflict resolved; any
non-zero exit leaves the conflict in place so the normal conflict-resolution path
(a fix task) handles it. Stdlib only, no dependency on the rest of the backend.
"""

import json
import sys


def _merge_section(ours: dict, theirs: dict) -> dict:
    """Max-cap union of two cap dicts.

    Key order: ours first (preserving its order), then any theirs-only keys
    appended in their order — deterministic regardless of merge direction once
    combined with the symmetric max on shared keys.
    """
    merged: dict = {}
    for key, val in ours.items():
        if key in theirs:
            other = theirs[key]
            if isinstance(val, (int, float)) and isinstance(other, (int, float)):
                merged[key] = max(val, other)
            elif isinstance(val, dict) and isinstance(other, dict):
                # Nested dict (shouldn't occur in practice) — recurse so we still
                # never lose a key. Non-numeric leaves prefer ours.
                merged[key] = _merge_section(val, other)
            else:
                # Type divergence — keep ours; the max-cap invariant only applies
                # to numeric caps.
                merged[key] = val
        else:
            merged[key] = val
    for key, val in theirs.items():
        if key not in merged:
            merged[key] = val
    return merged


def merge_limits(ours: dict, theirs: dict) -> dict:
    """Max-cap union merge of two whole module_size_limits.json documents.

    Top-level: ``_doc`` (a string) is kept from ours, falling back to theirs;
    each section dict (backend/agent/frontend, or any future section) is merged
    key-by-key taking the larger cap and the union of keys.
    """
    result: dict = {}
    for key, val in ours.items():
        if key in theirs:
            other = theirs[key]
            if isinstance(val, dict) and isinstance(other, dict):
                result[key] = _merge_section(val, other)
            else:
                # Scalar (e.g. _doc) — ours wins; both sides describe the same
                # policy so the text is interchangeable.
                result[key] = val
        else:
            result[key] = val
    for key, val in theirs.items():
        if key not in result:
            result[key] = val
    return result


def _load(path: str) -> dict:
    # Strict utf-8, not the host locale codec: git hands this driver three
    # blobs and the resolved bytes are committed, so a locale-dependent read
    # would make the merge result depend on which developer ran it.
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object, got {type(data).__name__}")
    return data


def _write(path: str, data: dict) -> None:
    """Write byte-identically to check_size_limits.write_limits.

    That promise is why ``encoding="utf-8", newline=""`` is spelled out rather
    than inherited from the host: without them a Windows resolve emits CRLF
    while ``--fix`` on the same file emits LF, so the two writers of this one
    ledger would disagree on every line and each merge would re-conflict.
    """
    with open(path, "w", encoding="utf-8", newline="") as f:
        json.dump(data, f, indent=2, ensure_ascii=True)
        f.write("\n")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 3:
        # Misconfigured driver invocation — leave the conflict for the normal path.
        print(
            "merge_size_limits: expected %O %A %B paths (got "
            f"{len(args)} args); leaving conflict unresolved",
            file=sys.stderr,
        )
        return 1
    _ancestor_path, ours_path, theirs_path = args[0], args[1], args[2]

    try:
        ours = _load(ours_path)
        theirs = _load(theirs_path)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        # Malformed input on either side — fall back to the normal conflict path
        # rather than risk emitting a lossy or invalid resolution.
        print(
            f"merge_size_limits: cannot parse inputs ({e}); leaving conflict unresolved",
            file=sys.stderr,
        )
        return 1

    merged = merge_limits(ours, theirs)
    # %A is both our version and the output path git reads the resolution from.
    _write(ours_path, merged)
    return 0


if __name__ == "__main__":
    sys.exit(main())
