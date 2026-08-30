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
        assert set(entry) in ({
            "op",
            "alias",
            "description",
            "cli",
            "prompt",
            "internal_reason",
        }, {
            "op",
            "alias",
            "description",
            "cli",
            "prompt",
            "internal_reason",
            "input",
        }), f"unexpected manifest entry keys: {sorted(entry)}"
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
    memory = next(entry for entry in manifest if entry["op"] == "set_memory")
    assert {"expected_head_id", "supersedes_id"} <= set(memory["input"]["fields"])
    assert any("tagged handoff must include" in note for note in memory["input"]["notes"])


def test_create_direction_manifest_matches_backend_contract():
    """The agent's generated help and the backend validator share one envelope."""
    from app.strategic_op_contracts import CREATE_DIRECTION_FIELDS

    manifest = _load_manifest()
    create = next(entry for entry in manifest if entry["op"] == "create_direction")
    contract = create["input"]
    assert contract["kind"] == "json"
    assert set(contract["required"]) == {
        "text",
        "success_evidence",
        "retirement_condition",
    }
    assert set(contract["fields"]) == CREATE_DIRECTION_FIELDS
    assert any(
        "category is product|structural|mixed" in note
        for note in contract["notes"]
    )


def test_update_direction_manifest_matches_backend_contract():
    """The public update schema remains a validated subset of backend fields."""
    from app.strategic_op_contracts import UPDATE_DIRECTION_FIELDS

    manifest = _load_manifest()
    update = next(entry for entry in manifest if entry["op"] == "update_direction")
    contract = update["input"]
    assert set(contract["required"]) == {"direction_id"}
    assert set(contract["fields"]) == {
        "direction_id",
        "text",
        "priority",
        "status",
        "gap_id",
        "success_evidence",
        "retirement_condition",
        "rationale",
        "terminal_reason",
        "criterion_keys",
        "recommended_mode",
        "system_id",
        "conflicts_with",
        "cost_budget_usd",
    }
    assert set(contract["fields"]) <= UPDATE_DIRECTION_FIELDS
    assert any("cost_budget_usd" in note for note in contract["notes"])


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


_SC_PATH = _BACKEND_ROOT / "app" / "strategic_completion.py"


def _extract_fallback_op_loop_body(source: str) -> str:
    """Slice the fallback ``for op_entry in direction_ops:`` loop body."""
    lines = source.splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if "for op_entry in direction_ops:" in ln),
        None,
    )
    assert start is not None, "could not locate fallback op loop in strategic_completion.py"
    for_indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        if not ln.strip():
            continue
        ln_indent = len(ln) - len(ln.lstrip())
        if ln_indent <= for_indent:
            end = i
            break
    return "\n".join(lines[start:end])


def _fallback_dispatch_ops(body: str) -> set[str]:
    return set(re.findall(r'\bop\s*==\s*"([a-z_]+)"', body))


def test_fallback_dispatch_accepts_exactly_the_portfolio_ops():
    """The fallback op loop must handle exactly _VALID_PORTFOLIO_OPS — every
    portfolio op has a live branch, and no op is silently dropped."""
    from app.strategic_completion import _VALID_PORTFOLIO_OPS

    source = _SC_PATH.read_text(encoding="utf-8")
    body = _extract_fallback_op_loop_body(source)
    dispatch_ops = _fallback_dispatch_ops(body)

    missing = _VALID_PORTFOLIO_OPS - dispatch_ops
    extra = dispatch_ops - _VALID_PORTFOLIO_OPS
    assert not missing, (
        "portfolio ops with no handler in fallback op loop "
        f"(add elif op == '<op>': branch in strategic_completion.py): {sorted(missing)}"
    )
    assert not extra, (
        "fallback op loop branches not declared in _VALID_PORTFOLIO_OPS: "
        f"{sorted(extra)}"
    )


def test_challenge_vocabulary_covers_activate_direction():
    """activate_direction must be in the backend challenge portfolio-op vocabulary
    so the challenge gate fails closed (not open) for a rejected activation."""
    from app.strategic_completion import _VALID_PORTFOLIO_OPS, _HARD_BLOCK_PORTFOLIO_OPS, _PORTFOLIO_OP_ALIASES

    assert "activate_direction" in _VALID_PORTFOLIO_OPS, (
        "activate_direction missing from _VALID_PORTFOLIO_OPS — challenge gate always passes"
    )
    assert "activate_direction" in _HARD_BLOCK_PORTFOLIO_OPS, (
        "activate_direction missing from _HARD_BLOCK_PORTFOLIO_OPS — a rejected challenge cannot block it"
    )
    assert _PORTFOLIO_OP_ALIASES.get("activate") == "activate_direction", (
        "activate alias missing from _PORTFOLIO_OP_ALIASES — short form never resolves"
    )
    manifest_ops = {e["op"] for e in _load_manifest()}
    assert "activate_direction" in manifest_ops, (
        "activate_direction not found in strategic_ops_manifest.json — coherence gap"
    )


def test_spawn_task_exposes_sequenced_landing_gate_fields():
    """The spawn_task discoverability contract must expose both sequenced
    landing-gate fields, and ``strategic_operation`` must still accept and mirror
    them into the spawned payload.

    ``require_deps_landed_on`` / ``release_base_branch`` are implemented in the
    backend (sequenced_spawn.landing_gate + the router mirroring) but previously
    appeared nowhere in the agent runtime, so the strategist could not author
    build-on-top sequences. This guard fails on EITHER side drifting alone: a
    registry/manifest edit that drops a field without a router change, or a
    router edit that drops the mirror without the manifest, both fail here.
    """
    manifest = _load_manifest()
    spawn = next(entry for entry in manifest if entry["op"] == "spawn_task")
    contract = spawn["input"]
    assert contract["kind"] == "json"
    fields = set(contract["fields"])
    assert "require_deps_landed_on" in fields, (
        "spawn_task manifest input is missing require_deps_landed_on — add it to "
        "INPUTS.spawn_task in agent/src/handlers/strategic-ops-registry.ts and "
        "regenerate backend/app/strategic_ops_manifest.json"
    )
    assert "release_base_branch" in fields, (
        "spawn_task manifest input is missing release_base_branch — add it to "
        "INPUTS.spawn_task in agent/src/handlers/strategic-ops-registry.ts and "
        "regenerate backend/app/strategic_ops_manifest.json"
    )
    notes = "\n".join(contract["notes"])
    assert "require_deps_landed_on" in notes and "release_base_branch" in notes, (
        "spawn_task manifest notes do not teach the ordered landing-gate workflow — "
        "add a note explaining require_deps_landed_on + release_base_branch to "
        "INPUTS.spawn_task and regenerate the manifest"
    )
    body = _extract_strategic_operation_body(
        _ROUTER_PATH.read_text(encoding="utf-8")
    )
    # The router reads both keys (via the seq-key loop) and mirrors both into
    # spawn_payload; a router-only drift that drops either surface fails here.
    assert '"require_deps_landed_on"' in body and '"release_base_branch"' in body, (
        "strategic_operation no longer reads one of require_deps_landed_on / "
        "release_base_branch from spawn data"
    )
    assert 'spawn_payload["require_deps_landed_on"]' in body, (
        "strategic_operation no longer mirrors require_deps_landed_on into spawn_payload"
    )
    assert 'spawn_payload["release_base_branch"]' in body, (
        "strategic_operation no longer mirrors release_base_branch into spawn_payload"
    )
