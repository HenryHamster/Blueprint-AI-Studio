"""Structural guard: timestamps on the HTTP response boundary must carry a zone.

Everything inside this app stores and passes **naive UTC** (``time_utils._utcnow``
returns ``datetime.now(timezone.utc).replace(tzinfo=None)``, and the
``NaiveUTCDateTime`` column type stores TIMESTAMPTZ but hands Python back a naive
datetime). A hand-written ``value.isoformat()`` therefore puts
``2026-08-24T12:34:56.789012`` on the wire -- a string that states no zone at all.

Both classes of consumer get a *wrong answer* rather than an error:

* Python callers comparing it against an aware ``now`` raise
  ``TypeError: can't subtract offset-naive and offset-aware datetimes``, or --
  worse -- assume local time and compute a delta that is wrong by the host's UTC
  offset.
* ``new Date(s)`` in JavaScript reads an offset-less ISO string as *local* time
  (ES2016+), silently shifting every rendered timestamp. The admin UI is the
  direct victim.

``app.time_utils.utc_iso`` is the one correct renderer. This module is the
ratchet that keeps bare ``.isoformat()`` off the response boundary.

Scope
-----
The scan covers the modules that build HTTP response bodies: the request
handlers (``backend/app/routers/*.py``, ``backend/app/admin.py``,
``backend/app/admin_helpers.py``) plus the projection modules a router returns
more or less verbatim (see ``_WIRE_MODULE_GLOBS``). Within those, it only flags
an ``.isoformat()`` whose value can reach a ``return`` -- see
:func:`collect_naive_isoformat_offenders` for the taint rule and its documented
blind spots.

It is deliberately *not* all of ``backend/app``: most ``.isoformat()`` calls in
this package write persisted JSON, prompt text or log lines, where attaching an
offset is a data-format change rather than a fix.

Waivers
-------
A site that genuinely must stay zone-less carries the reason *on the same source
line* as the call::

    meta["claimed_at"] = now.isoformat()  # utc-iso-ok: persisted JSON round-trip, not a wire field

Deliberately an inline marker and not an allowlist file: an allowlist grows
silently and reviews as a one-line diff, whereas the reason has to be written
next to the code it excuses and shows up in the diff that adds the site.
"""

from __future__ import annotations

import ast
import glob
import os
import re
from datetime import datetime, timedelta, timezone
from textwrap import dedent

from testing.fixgate_attribution import format_offender_marker

from app.time_utils import utc_iso


# -- Scope and scanner constants ----------------------------------------

_APP_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))

#: Modules whose job is to build HTTP response bodies.
#:
#: The first three define (or directly back) the request handlers. The rest are
#: projection modules a router imports and returns more or less verbatim, so a
#: bare ``.isoformat()`` there lands on the wire just the same. This list is
#: deliberately NOT "everything under app/": most of the ~400 other
#: ``.isoformat()`` calls in this package write persisted JSON, prompt text or
#: log lines, where a zone suffix would be a data-format change rather than a
#: fix. Add a module here when a router starts returning its output.
_WIRE_MODULE_GLOBS = (
    "routers/*.py",
    "admin.py",
    "admin_helpers.py",
    "project_message_projection.py",
    "workbench_events.py",
    "strategic_consultation_detail.py",
    "strategic_consultation_status.py",
    "evergreen_backlog.py",
)

#: Inline waiver marker. Must appear on the first or last physical line of the
#: offending call, together with a reason.
_WAIVER_MARKER = "utc-iso-ok:"

#: Receiver expressions that are already tz-aware, so ``.isoformat()`` on them
#: already emits an offset. Matched on unparsed source, not resolved types --
#: see the "blind spots" note in :func:`collect_naive_isoformat_offenders`.
_AWARE_RECEIVER_SOURCES = frozenset(
    {
        "datetime.now(timezone.utc)",
        "datetime.now(tz=timezone.utc)",
        "datetime.now(UTC)",
        "datetime.now(tz=UTC)",
        "datetime.utcnow()",
    }
)

#: Method names that push a value into an already-tainted container.
_CONTAINER_MUTATORS = frozenset({"append", "extend", "insert", "update"})

#: Fixpoint bound for the taint propagation below. Chains longer than this are
#: under-approximated (missed), never over-approximated (false-flagged).
_TAINT_ROUNDS = 8

_WHITESPACE = re.compile(r"\s+")


def _root_name(node: ast.AST) -> str | None:
    """Return the base ``ast.Name`` id under a chain of attributes/subscripts.

    ``rows[3]["a"].b`` -> ``"rows"``. Returns ``None`` when the base is not a
    plain name (a call result, a literal, ...), which the caller treats as
    "cannot attribute to a local variable".
    """
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _local_body(func: ast.AST) -> list[ast.AST]:
    """Nodes belonging to ``func`` itself, excluding nested function bodies.

    Nested defs are scanned separately as their own functions, so their returns
    must not seed the enclosing function's wire set.
    """
    out: list[ast.AST] = []

    def rec(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                and child is not func
            ):
                continue
            out.append(child)
            rec(child)

    rec(func)
    return out


def _load_names(node: ast.AST) -> set[str]:
    return {
        sub.id
        for sub in ast.walk(node)
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
    }


def _assign_targets(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        return [node.target]
    return []


def _is_container_mutation(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _CONTAINER_MUTATORS
    )


def _describe(node: ast.AST) -> str:
    """Single-line, bounded rendering of an expression for the offender string.

    ``format_offender_marker`` rejects empty or multi-line offender entries, so
    the receiver source is normalised here rather than at the call site.
    """
    text = _WHITESPACE.sub(" ", ast.unparse(node)).strip()
    return text if len(text) <= 80 else text[:77] + "..."


def collect_naive_isoformat_offenders(
    source: str, *, filename: str, rel: str
) -> list[str]:
    """Return offender strings for ``.isoformat()`` calls that reach a ``return``.

    The analysis is a small intra-function taint pass:

    1. Seed a per-function "wire set" with every loaded name appearing anywhere
       inside a ``return`` expression -- so ``return {"jobs": items}`` taints
       ``items``, not just a bare ``return items``.
    2. Grow it to a fixpoint: an assignment whose target's base name is already
       in the wire set pulls in every name on its right-hand side, and
       ``container.append(x)`` / ``.extend`` / ``.insert`` / ``.update`` pulls in
       every name in its arguments when the container is in the wire set. This is
       what catches two-hop assembly (``by_id[k].append({...})`` where ``by_id``
       is read from inside the returned structure).
    3. Every expression that feeds the wire set -- return values, tainted
       assignment right-hand sides, tainted mutation arguments -- is searched for
       ``<expr>.isoformat()``.

    A call is *not* an offender when its receiver source is a known aware
    expression, ends in ``.date()``, contains ``.astimezone(``, or the call
    carries an inline ``# utc-iso-ok: <reason>`` marker.

    Known blind spots -- deliberately under-approximating, so a miss is possible
    but a false accusation is not:

    * The pass is **intra-function**. A datetime that leaves through a mutated
      parameter, a module-level cache, or a helper the scanner cannot resolve is
      not reached.
    * Receiver *types* are never resolved. An ``ast.Attribute`` receiver
      (``row.created_at``) is assumed naive because that is what the ORM hands
      back here; an already-aware attribute would be flagged and needs a waiver.
      This is the same limitation called out on PR #17864: the scanner handles
      the resolvable cases and is explicit about the rest.
    * Chains longer than :data:`_TAINT_ROUNDS` hops are not followed.
    """
    tree = ast.parse(source, filename=filename)
    lines = source.splitlines()
    offenders: list[str] = []

    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    for func in functions:
        body = _local_body(func)

        wire: set[str] = set()
        for node in body:
            if isinstance(node, ast.Return) and node.value is not None:
                wire |= _load_names(node.value)

        for _ in range(_TAINT_ROUNDS):
            before = len(wire)
            for node in body:
                targets = _assign_targets(node)
                if targets and getattr(node, "value", None) is not None:
                    if any(_root_name(t) in wire for t in targets):
                        wire |= _load_names(node.value)
                elif (
                    _is_container_mutation(node)
                    and _root_name(node.func.value) in wire
                ):
                    for arg in node.args:
                        wire |= _load_names(arg)
            if len(wire) == before:
                break

        tainted: list[ast.AST] = []
        for node in body:
            if isinstance(node, ast.Return) and node.value is not None:
                tainted.append(node.value)
                continue
            targets = _assign_targets(node)
            if targets and getattr(node, "value", None) is not None:
                if any(_root_name(t) in wire for t in targets):
                    tainted.append(node.value)
            elif (
                _is_container_mutation(node)
                and _root_name(node.func.value) in wire
            ):
                tainted.extend(node.args)

        seen: set[int] = set()
        for subtree in tainted:
            for node in ast.walk(subtree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "isoformat"
                ):
                    continue
                if id(node) in seen:
                    continue
                seen.add(id(node))

                receiver_src = ast.unparse(node.func.value)
                if (
                    receiver_src in _AWARE_RECEIVER_SOURCES
                    or receiver_src.endswith(".date()")
                    or ".astimezone(" in receiver_src
                ):
                    continue

                span = {node.lineno, node.end_lineno or node.lineno}
                waiver_text = " ".join(
                    lines[n - 1] for n in sorted(span) if 0 < n <= len(lines)
                )
                if _WAIVER_MARKER in waiver_text:
                    continue

                receiver = _describe(node.func.value)
                offenders.append(
                    f"  {rel}:{node.lineno} in {func.name}() - "
                    f"{receiver}.isoformat() reaches a returned value with no UTC "
                    f"offset; use utc_iso({receiver}) or add an inline "
                    f"'# {_WAIVER_MARKER} <reason>' on that line"
                )

    return offenders


def _wire_module_paths() -> list[tuple[str, str]]:
    paths: list[str] = []
    for pattern in _WIRE_MODULE_GLOBS:
        paths.extend(glob.glob(os.path.join(_APP_DIR, pattern)))
    return [
        (p, os.path.relpath(p, _APP_DIR).replace("\\", "/")) for p in sorted(set(paths))
    ]


# -- Behaviour anchor for the renderer the guard points at --------------


def test_utc_iso_stamps_the_offset_on_naive_input():
    """Literal pin: naive UTC in, ``+00:00`` on the wire.

    Pinned as a literal rather than recomputed from ``utc_iso`` itself, so a
    change to the helper's output format has to be defended here explicitly.
    """
    naive = datetime(2026, 1, 2, 3, 4, 5, 678901)  # noqa: DTZ001 -- naive UTC is the input this helper exists to fix
    assert utc_iso(naive) == "2026-01-02T03:04:05.678901+00:00"


def test_utc_iso_normalises_aware_input_to_utc():
    aware = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=9)))
    assert utc_iso(aware) == "2026-01-01T18:04:05+00:00"


def test_utc_iso_passes_none_through():
    assert utc_iso(None) is None


def test_bare_isoformat_is_the_defect_this_guard_exists_for():
    """The thing being banned: no zone, so JS reads it as local time."""
    naive = datetime(2026, 1, 2, 3, 4, 5)  # noqa: DTZ001 -- the naive value is the defect being demonstrated
    assert naive.isoformat() == "2026-01-02T03:04:05"


# -- Unit tests for the scanner -----------------------------------------
#
# These use invented field names that appear nowhere in the app tree, so the
# scanner is proved against the *property* rather than against the specific
# call sites that were repaired alongside it.


def _scan(source: str) -> list[str]:
    return collect_naive_isoformat_offenders(
        dedent(source), filename="synthetic.py", rel="synthetic.py"
    )


def test_scan_flags_isoformat_in_a_returned_dict_literal():
    offenders = _scan(
        """
        async def get_beacon(beacon):
            return {
                "id": beacon.id,
                "lantern_lit_at": beacon.lantern_lit_at.isoformat(),
            }
        """
    )
    assert len(offenders) == 1
    assert "get_beacon()" in offenders[0]
    assert "beacon.lantern_lit_at" in offenders[0]


def test_scan_flags_isoformat_assembled_into_a_returned_local():
    """A value built into a local that the return reads is still on the wire."""
    offenders = _scan(
        """
        async def list_beacons(rows):
            payload = []
            for row in rows:
                payload.append({"lantern_lit_at": row.lantern_lit_at.isoformat()})
            return {"beacons": payload}
        """
    )
    assert len(offenders) == 1
    assert "list_beacons()" in offenders[0]


def test_scan_flags_two_hop_assembly_through_a_keyed_map():
    """The map is only read from *inside* the returned structure, one hop away."""
    offenders = _scan(
        """
        async def list_beacons(rows, beacons):
            trail_by_beacon = {}
            for row in rows:
                trail_by_beacon.setdefault(row.beacon_id, [])
                trail_by_beacon[row.beacon_id].append(
                    {"lantern_lit_at": row.lantern_lit_at.isoformat()}
                )
            return [
                {"id": b.id, "trail": trail_by_beacon.get(b.id, [])} for b in beacons
            ]
        """
    )
    assert len(offenders) == 1
    assert "row.lantern_lit_at" in offenders[0]


def test_scan_accepts_utc_iso():
    offenders = _scan(
        """
        async def get_beacon(beacon):
            return {"lantern_lit_at": utc_iso(beacon.lantern_lit_at)}
        """
    )
    assert offenders == []


def test_scan_accepts_an_inline_waiver_on_the_call_line():
    offenders = _scan(
        """
        async def get_beacon(beacon, meta):
            stamp = meta.beacon_seen_at.isoformat()  # utc-iso-ok: string-compared downstream
            return {"seen": stamp}
        """
    )
    assert offenders == []


def test_scan_requires_the_waiver_on_the_offending_line():
    """A marker parked elsewhere in the function does not silence a new site.

    This is what stops a waiver from drifting into a de-facto file-level
    allowlist as code is edited around it.
    """
    offenders = _scan(
        """
        async def get_beacon(beacon, meta):
            seen = meta.beacon_seen_at.isoformat()  # utc-iso-ok: string-compared downstream
            lit = beacon.lantern_lit_at.isoformat()
            return {"seen": seen, "lit": lit}
        """
    )
    assert len(offenders) == 1
    assert "beacon.lantern_lit_at" in offenders[0]


def test_scan_skips_receivers_that_are_already_aware():
    offenders = _scan(
        """
        async def get_beacon():
            return {"generated_at": datetime.now(timezone.utc).isoformat()}
        """
    )
    assert offenders == []


def test_scan_skips_dates_which_carry_no_zone():
    offenders = _scan(
        """
        async def get_beacon(row):
            return {"week_key": row.lantern_lit_at.date().isoformat()}
        """
    )
    assert offenders == []


def test_scan_ignores_isoformat_that_never_reaches_a_return():
    """Persisted-JSON writes are out of scope; only the wire is guarded."""
    offenders = _scan(
        """
        async def touch_beacon(beacon, now):
            meta = dict(beacon.meta or {})
            meta["beacon_seen_at"] = now.isoformat()
            beacon.meta = meta
            return {"ok": True}
        """
    )
    assert offenders == []


def test_scan_does_not_leak_nested_function_returns_into_the_parent():
    offenders = _scan(
        """
        async def get_beacon(beacon):
            def render():
                return beacon.lantern_lit_at.isoformat()
            beacon.meta = {"cached": render}
            return {"ok": True}
        """
    )
    assert len(offenders) == 1
    assert "render()" in offenders[0]


def test_scan_documents_its_intra_function_blind_spot():
    """Honest limitation: a value that escapes through a mutated *parameter* is
    not seen, because nothing in this function returns it.

    Recorded as a test so the gap is a stated property of the guard rather than
    an unexamined hole. Closing it would need cross-function analysis.
    """
    offenders = _scan(
        """
        def fill_beacon(out):
            out["lantern_lit_at"] = out["beacon"].lantern_lit_at.isoformat()
        """
    )
    assert offenders == []


def test_scan_offender_strings_are_marker_safe():
    """Offender entries must survive ``format_offender_marker`` (single-line)."""
    offenders = _scan(
        """
        async def get_beacon(beacon):
            return {
                "lantern_lit_at": (
                    beacon.lantern_lit_at
                    + timedelta(days=1)
                ).isoformat(),
            }
        """
    )
    assert len(offenders) == 1
    assert "\n" not in offenders[0]
    assert format_offender_marker(offenders).startswith("BLUEPRINT_OFFENDERS: ")


# -- Whole-boundary invariant -------------------------------------------


def test_wire_modules_are_actually_scanned():
    """Guard the guard: a glob that matches nothing would pass vacuously."""
    paths = _wire_module_paths()
    rels = {rel for _, rel in paths}
    assert len(paths) >= 30, f"wire-module glob collapsed to {len(paths)} file(s)"
    assert "admin.py" in rels
    assert "admin_helpers.py" in rels
    assert any(rel.startswith("routers/") for rel in rels)
    # Every non-router entry must resolve too -- a renamed projection module
    # would otherwise drop out of scope silently.
    for rel in _WIRE_MODULE_GLOBS:
        if "*" in rel:
            continue
        assert rel in rels, f"{rel} is listed in scope but does not exist"


def test_no_naive_isoformat_on_the_http_response_boundary():
    """Whole-boundary invariant: no bare ``.isoformat()`` reaches a response body.

    Fix an offender with ``app.time_utils.utc_iso(value)``, which stamps
    ``+00:00`` on the naive UTC this app stores. If the string genuinely must
    stay zone-less -- it is round-tripped through persisted JSON that an existing
    parser or a lexicographic comparison would break on -- put the reason inline
    on the same line as the call::

        meta["claimed_at"] = now.isoformat()  # utc-iso-ok: persisted JSON round-trip, not a wire field
    """
    all_offenders: list[str] = []
    for path, rel in _wire_module_paths():
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        all_offenders.extend(
            collect_naive_isoformat_offenders(source, filename=path, rel=rel)
        )

    assert not all_offenders, (
        "Found bare .isoformat() call(s) on the HTTP response boundary "
        "(backend/app/routers/*.py, admin.py, admin_helpers.py).\n"
        "This app stores NAIVE UTC, so .isoformat() emits an offset-less string. "
        "Python consumers raise TypeError or silently assume local time, and "
        "new Date(s) in the admin UI reads it as LOCAL time (ES2016+), shifting "
        "every rendered timestamp by the viewer's UTC offset.\n"
        "Fix: render with app.time_utils.utc_iso(value), or -- if the string is "
        "not a wire field -- add '# utc-iso-ok: <reason>' on the same line:\n"
        + "\n".join(all_offenders)
        + "\n"
        + format_offender_marker(all_offenders)
    )
