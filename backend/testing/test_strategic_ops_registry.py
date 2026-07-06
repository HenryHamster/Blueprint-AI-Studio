"""Cross-language drift-guard for the strategist mutating-op surface.

The strategist's mutating ops are declared in ONE canonical registry
(agent/src/handlers/strategic-ops-registry.ts) and mirrored into a checked-in
shared manifest (backend/app/strategic_ops_manifest.json). This test is the
BACKEND half of the drift-guard: it asserts the dispatch chain in
``routers/admin_agent_ops.py`` (``strategic_operation``) accepts EXACTLY the
manifest's backend op names — every manifest op has a live handler, and every
accepted op is in the manifest (no silent additions). The AGENT half
(strategic-ops-registry.test.ts) keeps the CLI/prompt surfaces and the manifest
itself in lockstep with the TypeScript registry.

Pure source-parse + JSON-load: no DB fixtures, so it runs in the standalone
smoke set too.
"""

import json
import re
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST_PATH = _BACKEND_ROOT / "app" / "strategic_ops_manifest.json"
_ROUTER_PATH = _BACKEND_ROOT / "app" / "routers" / "admin_agent_ops.py"


def _load_manifest() -> list[dict]:
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def _extract_strategic_operation_body(source: str) -> str:
    """Slice the ``strategic_operation`` function body out of the router source.

    The function runs from its ``async def`` line to the next top-level
    construct (``def``/``async def``/``class``/decorator at column 0). Slicing
    keeps the parser from picking up the ``payload.op`` branches in the
    neighbouring design-doc / playbook handlers.
    """
    lines = source.splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if ln.startswith("async def strategic_operation(")),
        None,
    )
    assert start is not None, "could not locate async def strategic_operation( in router"
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if re.match(r"^(async def |def |class |@)", lines[i]):
            end = i
            break
    return "\n".join(lines[start:end])


def _dispatch_ops(body: str) -> set[str]:
    """All ops the dispatch chain branches on (``payload.op == "<op>"``)."""
    return set(re.findall(r'payload\.op\s*==\s*"([a-z_]+)"', body))


def test_manifest_is_wellformed():
    manifest = _load_manifest()
    assert isinstance(manifest, list) and manifest, "manifest must be a non-empty list"
    seen: set[str] = set()
    for entry in manifest:
        assert set(entry) == {
            "op",
            "alias",
            "description",
            "cli",
            "prompt",
            "internal_reason",
        }, f"unexpected manifest entry keys: {sorted(entry)}"
        op = entry["op"]
        assert isinstance(op, str) and re.fullmatch(r"[a-z][a-z_]*", op), op
        assert op not in seen, f"duplicate op in manifest: {op}"
        seen.add(op)
        # Surfaced ops carry a kebab alias; internal-only ops carry none and a reason.
        if entry["cli"] or entry["prompt"]:
            assert entry["alias"] == op.replace("_", "-"), op
            assert entry["internal_reason"] is None, op
        else:
            assert entry["alias"] is None, op
            assert entry["internal_reason"], f"{op} must state why it is excluded"


def test_dispatch_accepts_exactly_the_manifest_ops():
    """The dispatch chain and the manifest must list the SAME op set — no more,
    no less. A backend op with no manifest entry (silent addition) or a manifest
    op with no live handler both fail here."""
    manifest_ops = {e["op"] for e in _load_manifest()}
    dispatch_ops = _dispatch_ops(_extract_strategic_operation_body(_ROUTER_PATH.read_text(encoding="utf-8")))

    missing_handler = manifest_ops - dispatch_ops
    silent_addition = dispatch_ops - manifest_ops
    assert not missing_handler, (
        f"manifest ops with no dispatch handler in strategic_operation: {sorted(missing_handler)}"
    )
    assert not silent_addition, (
        "dispatch ops missing from strategic_ops_manifest.json (add them to the "
        f"canonical registry and regenerate the manifest): {sorted(silent_addition)}"
    )
    assert manifest_ops == dispatch_ops
