"""Modularity tripwire for ``app.release_qualifier``.

The qualifier keeps re-exporting symbols that are now owned by sibling
modules so historical imports and monkeypatch targets keep working. This
tripwire fails loudly if a future edit re-defines one of those extracted
symbols inside ``release_qualifier.py`` or if the re-export stops resolving to
the canonical implementation in ``app.release_reconciliation``.
"""

from __future__ import annotations

import ast
import importlib
import os

import pytest


RELEASE_QUALIFIER_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "app", "release_qualifier.py")
)


EXTRACTED_SYMBOLS: dict[str, tuple[str, ...]] = {
    "app.release_reconciliation": (
        "_reconcile_if_pending_auto_merge_landed",
        "_reconcile_if_promotion_failed_landed",
    ),
    "app.release_qualifier_ci": (
        "_resolve_candidate_ci",
        "_resolve_ci_at_finalize",
    ),
    "app.release_qualifier_infra": (
        "_MIN_INFRA_ERROR_STREAK",
        "_handle_infra_failure",
    ),
    "app.release_qualifier_io": (
        "_spawn_release_audit_task",
        "_spawn_qualifier_smoketest_task",
        "_latest_artifact_logs",
        "get_latest_release_audit_report",
    ),
    "app.release_qualifier_readiness": (
        "_OUTCOME_LABELS",
        "_READINESS_CONFIDENCE_STDEV_MAX",
        "_READINESS_HISTORY_WINDOW",
        "_READINESS_MIN_CLEAN_STREAK",
        "_READINESS_MIN_VERDICTS",
        "_READINESS_MIN_WOULD_PROMOTE_RATE",
        "_READINESS_RECURRING_BLOCKER_MIN",
        "_empty_readiness",
        "compute_promotion_readiness",
        "format_qualification_for_api",
        "format_verdict_for_api",
    ),
    "app.release_qualifier_sweep": (
        "sweep_stuck_qualifications",
        "_force_finalize_timed_out_qualification",
        "_force_finalize_sweep_crash_loop",
        "_parse_started_at",
    ),
}


def _flatten_extracted_symbols() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for module, names in EXTRACTED_SYMBOLS.items():
        for name in names:
            pairs.append((module, name))
    return pairs


def _top_level_definitions(source: str) -> dict[str, str]:
    tree = ast.parse(source)
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            found.setdefault(node.name, "function")
        elif isinstance(node, ast.AsyncFunctionDef):
            found.setdefault(node.name, "async function")
        elif isinstance(node, ast.ClassDef):
            found.setdefault(node.name, "class")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.setdefault(target.id, "assignment")
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.setdefault(node.target.id, "assignment")
    return found


class TestExtractedSymbolsNotRedefinedInReleaseQualifier:
    def test_no_extracted_symbol_is_redefined(self):
        with open(RELEASE_QUALIFIER_PATH, encoding="utf-8") as fh:
            source = fh.read()
        defs = _top_level_definitions(source)

        violations: list[str] = []
        for module, name in _flatten_extracted_symbols():
            kind = defs.get(name)
            if kind is not None:
                violations.append(f"  {name} ({kind}) — canonical home: {module}")

        assert not violations, (
            "app/release_qualifier.py re-defines symbols that were extracted to "
            "a sibling module. This shadows the canonical implementation and "
            "re-grows the monolith.\n\n"
            "Re-defined symbols:\n"
            + "\n".join(violations)
            + "\n\nFix: move the definition back into its canonical sibling "
            "module and let release_qualifier re-export it via "
            "`from app.release_reconciliation import <name>  # noqa: F401`."
        )


class TestExtractedSymbolsImportableFromReleaseQualifier:
    @pytest.mark.parametrize(
        "source_module,symbol",
        _flatten_extracted_symbols(),
        ids=lambda v: v if isinstance(v, str) else "",
    )
    def test_symbol_importable_from_release_qualifier(
        self, source_module: str, symbol: str
    ):
        release_qualifier = importlib.import_module("app.release_qualifier")
        canonical = importlib.import_module(source_module)

        assert hasattr(release_qualifier, symbol), (
            f"app.release_qualifier.{symbol} is no longer importable. "
            f"Add the re-export at the top of release_qualifier.py:\n"
            f"    from {source_module} import {symbol}  # noqa: F401"
        )
        assert hasattr(canonical, symbol), (
            f"{source_module}.{symbol} is missing — the canonical home for "
            f"this extracted symbol no longer defines it. Either restore the "
            f"definition there, or remove it from EXTRACTED_SYMBOLS in this "
            f"test if the extraction was reverted intentionally."
        )

        release_qualifier_obj = getattr(release_qualifier, symbol)
        canonical_obj = getattr(canonical, symbol)
        assert release_qualifier_obj is canonical_obj, (
            f"app.release_qualifier.{symbol} is not the same object as "
            f"{source_module}.{symbol}. release_qualifier is shadowing the "
            f"canonical implementation — drop the local definition and rely "
            f"on the re-export (`from {source_module} import {symbol}  "
            f"# noqa: F401`)."
        )
