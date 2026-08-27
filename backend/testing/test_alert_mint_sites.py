"""Structural guard on raw ``Alert(...)`` construction under ``backend/app``.

WHY THIS EXISTS
---------------
``app.alert_policy.create_project_alert_if_allowed`` is the sanctioned way to
mint a project alert: it consults ``ALERT_EVENT_POLICIES`` for the event's
route, audience, domain and severity floor, and returns an ``AlertDecision``
carrying the REASON it allowed or suppressed the row.  Constructing
``Alert(...)`` directly bypasses all of that.

The bypass is how the alert feed filled with rows nothing could ever resolve.
A raw construction asks no question, so:

* an event type that was never registered mints anyway -- there is no policy,
  therefore no retirement mode, therefore no path out of the feed;
* an event registered ``TIMELINE_ONLY`` mints a durable row anyway, which the
  policy backstop then retires on its next pass, which the producer mints
  again -- permanent churn between two subsystems that each believe they are
  right;
* an event whose registered route or audience says "do not put this in the
  operator feed" lands in it regardless.

This module re-derives the census from the AST on every run, so a NEW bypass
fails CI at the commit that introduces it rather than months later on the
operator's screen.

WHY THE ALLOWLIST IS KEYED THE WAY IT IS
----------------------------------------
Keyed on ``(module, enclosing function qualname, event_type)`` with an
expected occurrence count -- never on line numbers.  Line-number keys go stale
on any edit ABOVE the site and generate pure-renumbering CI reds; see
``TestProblemModelWritePathInvariants._SAFE_SITES`` and the commit that
re-keyed it structurally for the same reason.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.alert_policy import (
    ALERT_EVENT_POLICIES,
    ROUTE_ALERT,
    AlertRetirementMode,
)
from app.message_audience import is_operator_visible, route_event

_APP_DIR = Path(__file__).resolve().parents[1] / "app"

#: ``create_project_alert_if_allowed`` IS the gateway -- it is the one place a
#: raw construction is correct, because it has already asked the policy.
_GATEWAY_SITE = ("alert_policy.py", "create_project_alert_if_allowed")

#: Raw sites whose ``event_type`` is not a string literal.  A dynamic event type
#: cannot be checked statically, so each one is listed deliberately: adding a
#: new dynamic site is a decision, not an accident.  Keyed structurally, value
#: is ``(expected_occurrences, reason)``.
_DYNAMIC_EVENT_TYPE_SITES: dict[tuple[str, str], tuple[int, str]] = {
    _GATEWAY_SITE: (
        1,
        "the policy gateway itself; the event type is its own argument and has "
        "already been checked against ALERT_EVENT_POLICIES above this line.",
    ),
    ("approved_pr_stranding.py", "_sweep_checkpoint_owners"): (
        1,
        "event type is selected from a local branch table in the sweep.",
    ),
    ("approved_pr_stranding.py", "sweep_approved_pr_stranding"): (
        1,
        "event type is selected from a local branch table in the sweep.",
    ),
    ("release_drift_sentinel_alerts.py", "raise_drift_sentinel_alert"): (
        1,
        "generic sentinel raiser; the event type is the caller's argument.",
    ),
}

#: Raw sites whose registered policy WOULD suppress the row if the mint were
#: routed through the gateway.  Every entry is a real, unresolved contradiction
#: between a mint site and the registry -- listing it here does not bless it,
#: it records that converting the site would DELETE a signal rather than move
#: it to another surface, which is a separate decision each producer is owed.
#:
#: Keyed ``(module, enclosing qualname, event_type)`` -> ``(count, reason)``.
_POLICY_BYPASS_ALLOWLIST: dict[tuple[str, str, str], tuple[int, str]] = {
    (
        "alert_dispatch.py",
        "_maybe_create_migration_slot_contention_alert",
        "migration.slot_contention",
    ): (
        1,
        "strategist-internal audience: the row is filtered out of the operator "
        "feed on READ, and the strategist consumes it. DB bloat, not feed noise.",
    ),
    (
        "alert_dispatch.py",
        "_maybe_create_unpriced_models_alert",
        "cost.unpriced_models",
    ): (
        1,
        "registered ROUTE_HEALTH; routing the mint would suppress it outright "
        "and this emitter has no other observable.",
    ),
    (
        "alert_dispatch.py",
        "_maybe_create_self_repair_burn_share_alert",
        "efficiency.self_repair_burn_share",
    ): (1, "strategist-internal audience; read-filtered from the operator feed."),
    (
        "alert_dispatch.py",
        "_maybe_create_stale_staged_direction_alert",
        "research.stale_staged_direction",
    ): (1, "strategist-internal audience; read-filtered from the operator feed."),
    (
        "direction_lifecycle.py",
        "_process_direction_lifecycle_locked",
        "research.retirement_consideration_needed",
    ): (1, "strategist-internal audience; read-filtered from the operator feed."),
    (
        "direction_lifecycle.py",
        "_process_direction_lifecycle_locked",
        "research.churn_retirement_recommended",
    ): (1, "strategist-internal audience; read-filtered from the operator feed."),
    (
        "hotfix_backmerge.py",
        "_emit_size_violation_alerts",
        "hotfix_backmerge.size_violation",
    ): (
        2,
        "NOT a live bypass: _emit_size_violation_alerts early-returns on the "
        "same registry audience check before reaching either construction.",
    ),
    (
        "pipeline_alert_scan.py",
        "_run_signal_staleness_watchdog",
        "sr.signal_stale",
    ): (
        2,
        "registered ROUTE_HEALTH while its only producer mints durable rows. "
        "The contradiction is a real defect owed a decision (give the condition "
        "a resolver and register ROUTE_ALERT, or retire the emitter); routing "
        "the mint today would silently delete the staleness signal.",
    ),
    (
        "research_direction_state.py",
        "apply_post_spawn_state",
        "research.dedup_deadlock",
    ): (1, "strategist-internal audience; read-filtered from the operator feed."),
    (
        "research_direction_state.py",
        "apply_post_spawn_state",
        "research.already_fixed_deadlock",
    ): (1, "strategist-internal audience; read-filtered from the operator feed."),
    (
        "warm_pool_maintainer.py",
        "_maybe_emit_cost_guard_alert",
        "warm_pool.cost_guard",
    ): (1, "strategist-internal audience; read-filtered from the operator feed."),
}


@dataclass(frozen=True)
class RawMintSite:
    module: str
    qualname: str
    event_type: str | None
    line: int

    @property
    def literal_key(self) -> tuple[str, str, str]:
        assert self.event_type is not None
        return (self.module, self.qualname, self.event_type)

    @property
    def site_key(self) -> tuple[str, str]:
        return (self.module, self.qualname)


def _enclosing_qualnames(tree: ast.AST) -> dict[int, str]:
    """Map every line to its innermost enclosing def/class qualname."""
    out: dict[int, str] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                name = f"{prefix}.{child.name}" if prefix else child.name
                for sub in ast.walk(child):
                    out.setdefault(getattr(sub, "lineno", -1), name)
                walk(child, name)
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


def _collect_raw_mint_sites() -> list[RawMintSite]:
    """Every ``Alert(...)`` construction under ``backend/app``."""
    sites: list[RawMintSite] = []
    for path in sorted(_APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owners = _enclosing_qualnames(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "Alert"):
                continue
            event_type: str | None = None
            for keyword in node.keywords:
                if keyword.arg != "event_type":
                    continue
                if isinstance(keyword.value, ast.Constant) and isinstance(
                    keyword.value.value, str
                ):
                    event_type = keyword.value.value
            sites.append(
                RawMintSite(
                    module=str(path.relative_to(_APP_DIR)).replace("\\", "/"),
                    qualname=owners.get(node.lineno, "<module>"),
                    event_type=event_type,
                    line=node.lineno,
                )
            )
    return sites


def _suppression_reason(event_type: str) -> str | None:
    """Why the policy gateway would refuse this event, or ``None`` if it allows."""
    policy = ALERT_EVENT_POLICIES.get(event_type)
    if policy is None:
        return None
    audience = policy.audience or route_event(event_type)
    if not is_operator_visible(audience):
        return f"audience:{audience}"
    if policy.route != ROUTE_ALERT:
        return f"route:{policy.route}"
    return None


@pytest.fixture(scope="module")
def raw_sites() -> list[RawMintSite]:
    return _collect_raw_mint_sites()


def test_the_census_actually_finds_raw_mint_sites(raw_sites):
    """Anti-vacuity: a broken walker must fail here, not silently pass below.

    Every other test in this module is an assertion over ``raw_sites``.  If the
    AST walk ever returns an empty (or near-empty) list -- a renamed model, a
    changed import style, a wrong app directory -- all of them would pass while
    checking nothing.  That is exactly the failure mode this whole module was
    written to eliminate, so it is pinned here first.
    """
    assert len(raw_sites) > 50, (
        "the raw-mint census collapsed; the guards below would be vacuous. "
        f"found {len(raw_sites)} sites under {_APP_DIR}"
    )
    # The sanctioned gateway is a known-present raw construction. If the walker
    # cannot see this one, it cannot see any of them.
    assert any(
        site.site_key == _GATEWAY_SITE for site in raw_sites
    ), "the census cannot see the alert_policy gateway's own Alert(...) call"
    assert any(site.event_type is not None for site in raw_sites)


def test_every_raw_mint_site_uses_a_registered_event_type(raw_sites):
    """An unregistered event has no policy, so no retirement mode, so no exit."""
    unregistered = sorted(
        {
            f"{site.module}:{site.line} {site.qualname} -> {site.event_type}"
            for site in raw_sites
            if site.event_type is not None
            and site.event_type not in ALERT_EVENT_POLICIES
        }
    )
    assert not unregistered, (
        "raw Alert(...) sites minting event types absent from "
        "ALERT_EVENT_POLICIES -- each one is a row that can never be retired "
        "because no policy declares how it ends:\n  " + "\n  ".join(unregistered)
    )


def test_dynamic_event_type_mint_sites_are_declared(raw_sites):
    """A dynamic event type cannot be checked statically, so it is declared."""
    found: dict[tuple[str, str], int] = {}
    for site in raw_sites:
        if site.event_type is None:
            found[site.site_key] = found.get(site.site_key, 0) + 1

    undeclared = sorted(key for key in found if key not in _DYNAMIC_EVENT_TYPE_SITES)
    assert not undeclared, (
        "new raw Alert(...) site with a non-literal event_type. Prefer "
        "create_project_alert_if_allowed; if the site is genuinely generic, add "
        "it to _DYNAMIC_EVENT_TYPE_SITES with a reason:\n  "
        + "\n  ".join(f"{module} {qualname}" for module, qualname in undeclared)
    )

    stale = sorted(key for key in _DYNAMIC_EVENT_TYPE_SITES if key not in found)
    assert not stale, (
        "_DYNAMIC_EVENT_TYPE_SITES entries that no longer match any site; "
        "delete them:\n  " + "\n  ".join(f"{m} {q}" for m, q in stale)
    )

    miscounted = sorted(
        f"{module} {qualname}: declared "
        f"{_DYNAMIC_EVENT_TYPE_SITES[(module, qualname)][0]}, found {count}"
        for (module, qualname), count in found.items()
        if _DYNAMIC_EVENT_TYPE_SITES[(module, qualname)][0] != count
    )
    assert not miscounted, (
        "_DYNAMIC_EVENT_TYPE_SITES occurrence counts drifted:\n  "
        + "\n  ".join(miscounted)
    )


def test_no_undeclared_raw_mint_site_bypasses_a_suppressing_policy(raw_sites):
    """A raw mint of an event the policy would suppress is a bypass."""
    found: dict[tuple[str, str, str], int] = {}
    reasons: dict[tuple[str, str, str], str] = {}
    for site in raw_sites:
        if site.event_type is None:
            continue
        reason = _suppression_reason(site.event_type)
        if reason is None:
            continue
        key = site.literal_key
        found[key] = found.get(key, 0) + 1
        reasons[key] = reason

    undeclared = sorted(key for key in found if key not in _POLICY_BYPASS_ALLOWLIST)
    assert not undeclared, (
        "raw Alert(...) site minting an event whose registered policy would "
        "SUPPRESS it. Route the mint through create_project_alert_if_allowed, "
        "or -- if routing it would delete the signal rather than move it -- fix "
        "the registration and record the decision in _POLICY_BYPASS_ALLOWLIST "
        "with a reason:\n  "
        + "\n  ".join(
            f"{module} {qualname} -> {event} ({reasons[(module, qualname, event)]})"
            for module, qualname, event in undeclared
        )
    )


def test_policy_bypass_allowlist_has_no_stale_entries(raw_sites):
    """A fixed producer must drop out of the allowlist, not linger in it.

    Without this direction the allowlist only ever grows, and a resolved
    contradiction keeps looking unresolved.
    """
    found: dict[tuple[str, str, str], int] = {}
    for site in raw_sites:
        if site.event_type is None:
            continue
        if _suppression_reason(site.event_type) is None:
            continue
        key = site.literal_key
        found[key] = found.get(key, 0) + 1

    stale = sorted(key for key in _POLICY_BYPASS_ALLOWLIST if key not in found)
    assert not stale, (
        "_POLICY_BYPASS_ALLOWLIST entries matching no live suppressed mint "
        "site -- the producer was fixed or moved; delete the entry:\n  "
        + "\n  ".join(f"{m} {q} -> {e}" for m, q, e in stale)
    )

    miscounted = sorted(
        f"{m} {q} -> {e}: declared {_POLICY_BYPASS_ALLOWLIST[(m, q, e)][0]}, "
        f"found {count}"
        for (m, q, e), count in found.items()
        if (m, q, e) in _POLICY_BYPASS_ALLOWLIST
        and _POLICY_BYPASS_ALLOWLIST[(m, q, e)][0] != count
    )
    assert not miscounted, (
        "_POLICY_BYPASS_ALLOWLIST occurrence counts drifted -- a bypass was "
        "added or removed next to an existing one:\n  " + "\n  ".join(miscounted)
    )

    unreasoned = sorted(
        key
        for key, (_, reason) in _POLICY_BYPASS_ALLOWLIST.items()
        if not reason.strip()
    )
    assert not unreasoned, (
        "every bypass must state WHY it is not converted: "
        + ", ".join(f"{m} {q} -> {e}" for m, q, e in unreasoned)
    )


def test_no_timeline_only_event_has_a_live_raw_mint_site(raw_sites):
    """TIMELINE_ONLY + a live raw producer is permanent two-subsystem churn.

    ``sweep_resolve_policy_retirement_alerts`` retires durable rows for every
    TIMELINE_ONLY event on each pass.  If a producer still constructs those rows
    directly, the sweep retires and the producer re-mints, forever, and the
    retirement mode silently means nothing.  Membership in
    ``_TIMELINE_ONLY_RETIREMENT_EVENTS`` therefore REQUIRES zero raw mint sites.
    """
    timeline_only = {
        event_type
        for event_type, policy in ALERT_EVENT_POLICIES.items()
        if policy.retirement == AlertRetirementMode.TIMELINE_ONLY
    }
    assert timeline_only, "the registry must declare some TIMELINE_ONLY events"

    offenders = sorted(
        f"{site.module}:{site.line} {site.qualname} -> {site.event_type}"
        for site in raw_sites
        if site.event_type in timeline_only
    )
    assert not offenders, (
        "TIMELINE_ONLY events with a live raw mint site. The policy sweep will "
        "retire these rows and the producer will re-create them every pass. "
        "Route the mint through create_project_alert_if_allowed (which honours "
        "TIMELINE_ONLY) or drop the event from "
        "_TIMELINE_ONLY_RETIREMENT_EVENTS:\n  " + "\n  ".join(offenders)
    )
