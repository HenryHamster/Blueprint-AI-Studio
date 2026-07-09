// Shared static prompt sub-blocks for the research and plan handlers.
//
// These strings are guidance the model needs in BOTH the research findings
// prompt and the plan decomposition prompt. Defining them here once — and
// referencing them from `research-plan.ts` — keeps the two prompts from drifting
// apart and removes the duplicated (and previously self-contradictory)
// acceptance_evidence documentation that was pasted into the research OUTPUT
// FORMAT twice with two incompatible vocabularies.

/**
 * The single canonical acceptance_evidence rubric.
 *
 * This is the FORWARD-evidence vocabulary the backend actually honors: the
 * payload/merge-gate parser (`AcceptanceEvidence` / `read_acceptance_evidence`
 * in `backend/app/payload_schemas.py`) accepts exactly these four kinds, and the
 * context parser (`parse_acceptance_evidence`) normalizes the same four kinds
 * onto the task context. The older `{"kind": "test"|"behavior"|"artifact",
 * "descriptor", "selector"}` shape is intentionally NOT documented here — a
 * finding emitted in that shape used to be silently dropped from the merge gate.
 *
 * Only the four-kind enumeration lives here; each prompt keeps its own framing
 * sentence (research: "FORWARD evidence…"; plan: "concrete evidence for THIS
 * subtask…") so the channel-specific wording is unchanged.
 */
/**
 * Shared capability-declaration framework documentation, embedded identically in
 * the research, plan, strategic, and design prompts so every task type
 * participates in the same vocabulary (not research special-casing).
 *
 * Frames both the producer stamp (`provides_capability`) and the consumer ref
 * (`prerequisite`) as drawn from the AVAILABLE CAPABILITIES list, using exact
 * matching. Off-registry refs are accepted (advisory) and never rejected.
 */
export const CAPABILITY_DECLARATION_DOC = [
  '**Shared capability vocabulary (applies to this and every other task type):** you may declare a `provides_capability` stamp (a capability your work ADVERTISES) and/or a `prerequisite` capability ref (a capability your work DEPENDS ON). BOTH must be drawn from the AVAILABLE CAPABILITIES list above when one fits — the registry is the shared namespace, and the resolver matches EXACTLY (no fuzzy matching), so a free-prose name a different lane will never reproduce leaves the dependency stranded. Use the SAME kebab-case convention and propose a NEW name ONLY for a genuinely new capability absent from the list. An off-registry ref is still accepted (never rejected) and flagged advisory for the strategist to fold into the registry.',
].join('\n');

export const ACCEPTANCE_EVIDENCE_RUBRIC =
  'Exactly one of five kinds: (1) {"kind": "named_test", "test_path": "tests/test_x.py", "test_id": "test_fn"} — a test exercising the changed seam (test_path must name a real test file; test_id optional, or inline as path::test_fn); (2) {"kind": "observable_behavior", "description": "..."} — a substantive (≥10 char, non-placeholder) description of a behavior to exercise; (3) {"kind": "downstream_artifact", "artifact_id": "..."} — an artifact a later task consumes; (4) {"kind": "visual", "component": "<UI component under test>", "viewport_width": <px, 1-4096>, "assertion_type": "<optional, e.g. overflow|text_truncation>"} — for FRONTEND-TOUCHING work (admin-ui components), proven by an uploaded viewport screenshot of that component captured at that width on a successful run; (5) {"kind": "coverage_delta", "threshold": <minimum coverage percentage, 0-100>} — an objective test-coverage measurement of total codebase line coverage (not changed-line-differential), computed by the test runner independently of any agent self-report. Falls back to test pass rate when line-coverage instrumentation is unavailable.';
