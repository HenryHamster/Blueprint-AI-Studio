"""Qualification harness for the production-shaped Memory V2 retrieval seam.

This is deliberately *not* a claim of public benchmark qualification.  The
fixture checks the failure classes our envelope must handle before a selected
project is allowed to receive V2 cards: exact mathematical recall, corrections,
conflict, abstention, project isolation, prompt injection, and deletion.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence


@dataclass(frozen=True)
class RetrievalPolicyMetadata:
    policy_id: str
    provider: str | None = None
    vector_required: bool = False
    paid_services_required: bool = False


@dataclass(frozen=True)
class ResearchMemEvalCase:
    case_id: str
    category: str
    query: str
    expected_exact_terms: tuple[str, ...]
    unavailable_terms: tuple[str, ...] = ()
    expected_answerable: bool = True
    require_envelope: bool = True


@dataclass(frozen=True)
class ResearchMemEvalCaseResult:
    case_id: str
    category: str
    exact_recall: float
    unavailable_leak: bool
    answerability_matches: bool
    envelope_matches: bool
    latency_ms: float
    estimated_cost_usd: float


@dataclass(frozen=True)
class ResearchMemEvalResult:
    policy: RetrievalPolicyMetadata
    cases: tuple[ResearchMemEvalCaseResult, ...]
    exact_recall: float
    unavailable_leak_rate: float
    p50_latency_ms: float
    p95_latency_ms: float
    estimated_cost_usd: float


@dataclass(frozen=True)
class ResearchMemEvalQualification:
    passed: bool
    failures: tuple[str, ...]
    result: ResearchMemEvalResult


# Production retrieval returns cards plus answerability/trace; the test seam is
# intentionally the same shape, instead of a synthetic lexical-only callback.
RetrievalFn = Callable[[ResearchMemEvalCase], Mapping[str, Any]]
AsyncRetrievalFn = Callable[[ResearchMemEvalCase], Awaitable[Mapping[str, Any]]]
LEXICAL_V2_POLICY = RetrievalPolicyMetadata(policy_id="lexical-v2")
REQUIRED_CATEGORIES = frozenset(
    {"exact_formula", "correction_history", "conflict", "abstention", "cross_project", "injection", "deletion"}
)


def load_research_mem_eval_fixture(path: Path) -> tuple[RetrievalPolicyMetadata, tuple[ResearchMemEvalCase, ...]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 2:
        raise ValueError("ResearchMemEval fixture must use schema_version 2")
    policy_payload = payload.get("policy", {})
    policy = RetrievalPolicyMetadata(
        policy_id=str(policy_payload.get("policy_id", LEXICAL_V2_POLICY.policy_id)),
        provider=policy_payload.get("provider"),
        vector_required=bool(policy_payload.get("vector_required", False)),
        paid_services_required=bool(policy_payload.get("paid_services_required", False)),
    )
    cases: list[ResearchMemEvalCase] = []
    for raw in payload.get("cases", []):
        case_id, category, query, terms = raw.get("case_id"), raw.get("category"), raw.get("query"), raw.get("expected_exact_terms")
        unavailable = raw.get("unavailable_terms", [])
        if not all(isinstance(value, str) and value for value in (case_id, category, query)):
            raise ValueError("Each case needs non-empty case_id, category, and query")
        if not isinstance(terms, list) or not all(isinstance(term, str) and term for term in terms):
            raise ValueError("expected_exact_terms must be a string list (empty is allowed for abstention)")
        if not isinstance(unavailable, list) or not all(isinstance(term, str) and term for term in unavailable):
            raise ValueError("unavailable_terms must be a string list")
        cases.append(ResearchMemEvalCase(case_id, category, query, tuple(terms), tuple(unavailable), bool(raw.get("expected_answerable", True)), bool(raw.get("require_envelope", True))))
    if not cases:
        raise ValueError("ResearchMemEval fixture must include cases")
    return policy, tuple(cases)


def _text(value: Any) -> str:
    if isinstance(value, Mapping):
        return "\n".join(_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(_text(item) for item in value)
    return str(value or "")


def _case_result(case: ResearchMemEvalCase, response: Mapping[str, Any], latency: float) -> ResearchMemEvalCaseResult:
    cards = response.get("evidence_cards", response.get("cards", ()))
    retrieved = _text(cards).casefold()
    envelope = _text(response.get("envelope_core", response.get("envelope", {}))).casefold()
    found = sum(term.casefold() in retrieved for term in case.expected_exact_terms)
    recall = found / len(case.expected_exact_terms) if case.expected_exact_terms else 1.0
    unavailable = any(term.casefold() in retrieved or term.casefold() in envelope for term in case.unavailable_terms)
    envelope_ok = (not case.require_envelope) or all(term.casefold() in envelope for term in case.expected_exact_terms)
    return ResearchMemEvalCaseResult(case.case_id, case.category, recall, unavailable, bool(response.get("answerable")) == case.expected_answerable, envelope_ok, latency, float(response.get("estimated_cost_usd", 0.0)))


def _aggregate(policy: RetrievalPolicyMetadata, results: Sequence[ResearchMemEvalCaseResult]) -> ResearchMemEvalResult:
    latencies = sorted(row.latency_ms for row in results)
    return ResearchMemEvalResult(policy, tuple(results), statistics.fmean(row.exact_recall for row in results), statistics.fmean(float(row.unavailable_leak) for row in results), statistics.median(latencies), latencies[max(0, int((len(latencies) - 1) * .95))], sum(row.estimated_cost_usd for row in results))


def run_research_mem_eval(cases: Sequence[ResearchMemEvalCase], retriever: RetrievalFn, *, policy: RetrievalPolicyMetadata = LEXICAL_V2_POLICY) -> ResearchMemEvalResult:
    results: list[ResearchMemEvalCaseResult] = []
    for case in cases:
        started = time.perf_counter()
        response = retriever(case)
        results.append(_case_result(case, response, (time.perf_counter() - started) * 1000))
    return _aggregate(policy, results)


async def run_research_mem_eval_async(cases: Sequence[ResearchMemEvalCase], retriever: AsyncRetrievalFn, *, policy: RetrievalPolicyMetadata = LEXICAL_V2_POLICY) -> ResearchMemEvalResult:
    """Run the same gate against a real async retrieval/envelope implementation."""

    results: list[ResearchMemEvalCaseResult] = []
    for case in cases:
        started = time.perf_counter()
        response = await retriever(case)
        results.append(_case_result(case, response, (time.perf_counter() - started) * 1000))
    return _aggregate(policy, results)


def qualify_research_mem_eval(result: ResearchMemEvalResult, *, max_p95_latency_ms: float = 250.0, max_estimated_cost_usd: float = 0.0) -> ResearchMemEvalQualification:
    failures: list[str] = []
    if result.policy != LEXICAL_V2_POLICY:
        failures.append("qualification baseline must use provider-neutral lexical-v2")
    categories = {row.category for row in result.cases}
    missing = REQUIRED_CATEGORIES - categories
    if missing:
        failures.append("missing required categories: " + ", ".join(sorted(missing)))
    # Each case and category gates independently: aggregate averages are for
    # observability only and can never qualify a leak or failed abstention.
    for row in result.cases:
        if row.exact_recall != 1.0:
            failures.append(f"{row.case_id}: exact recall failed")
        if row.unavailable_leak:
            failures.append(f"{row.case_id}: unavailable content leaked")
        if not row.answerability_matches:
            failures.append(f"{row.case_id}: answerability mismatch")
        if not row.envelope_matches:
            failures.append(f"{row.case_id}: envelope did not preserve required evidence")
    if result.p95_latency_ms > max_p95_latency_ms:
        failures.append("p95 retrieval latency exceeded the qualification budget")
    if result.estimated_cost_usd > max_estimated_cost_usd:
        failures.append("estimated retrieval cost exceeded the qualification budget")
    return ResearchMemEvalQualification(not failures, tuple(failures), result)
