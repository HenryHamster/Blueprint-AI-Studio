"""Contract guard: agent audit artifact shape vs. backend score parser.

The agent's handleReleaseAudit handler emits a JSON artifact whose shape the
backend's _score_audit / _audit_missing_reason functions parse.  Both sides
currently agree on findings[].severity, but no test prevented drift.  These
tests pin that contract so any future rewrite of the agent prompt that renames
the key to 'issues'/'level' breaks a test immediately.
"""

import json
import os
import pathlib
import pytest

# Pure functions — no DB, no I/O.
from app.models import ArtifactType
from app.release_scoring import (
    _audit_missing_reason,
    _compute_audit_challenge_synthesis,
    _score_audit,
)

_FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "release_audit_sample.json"

_CHALLENGE_SAMPLE = {
    "agreement": "partial",
    "agreement_assessment": "The primary audit found one risk but missed another.",
    "missing_findings": [
        {
            "severity": "medium",
            "summary": "Missed release-risk finding.",
            "file": "backend/app/release_qualifier.py",
            "verification_note": "verified by reading the changed finalize path",
        }
    ],
    "overstated_findings": [
        {
            "severity": "low",
            "summary": "Primary overstates migration risk.",
            "file": "backend/app/models.py",
            "verification_note": "the cited schema column is not changed here",
        }
    ],
    "challenge_severity": "medium",
}


def _resolve_audits_ts_path() -> "str | None":
    """Locate ``agent/src/handlers/audits.ts`` across local + Docker envs.

    The release-audit prompt and its JSON schema block live in ``audits.ts``
    (moved out of ``specialized.ts`` when the agent handlers were decomposed
    into focused modules in #6597).  Mirrors ``_resolve_prompt_ts_path`` in
    test_invariants.py.  CI runs pytest inside the backend Docker image (built
    from ./backend only), so the agent tree is not on the filesystem unless
    ``scripts/test-backend.sh`` mounts it at ``/agent/src``.  Returns the first
    existing path or ``None`` to let the test skip rather than error.
    """
    env_override = os.getenv("AGENT_AUDITS_TS")
    candidates = []
    if env_override:
        candidates.append(env_override)
    candidates.append("/agent/src/handlers/audits.ts")
    candidates.append(
        os.path.normpath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "..",
                "agent",
                "src",
                "handlers",
                "audits.ts",
            )
        )
    )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


@pytest.fixture(scope="module")
def audit_fixture() -> dict:
    return json.loads(_FIXTURE_PATH.read_text())


class TestReleaseAuditFixtureShape:
    """The fixture itself is the canonical example of what the agent should emit."""

    def test_fixture_recognized_by_audit_missing_reason(self, audit_fixture):
        """_audit_missing_reason must return None — the shape is fully scoreable."""
        result = _audit_missing_reason(audit_fixture)
        assert result is None, (
            f"_audit_missing_reason returned {result!r} for the canonical fixture; "
            "the fixture shape is no longer recognized as scoreable."
        )

    def test_fixture_score_audit_yields_high_audit_score(self, audit_fixture):
        """_score_audit must yield audit_score >= 0.7 for the canonical fixture."""
        audit_score, _erosion = _score_audit(audit_fixture)
        assert audit_score is not None, (
            "_score_audit returned None for the canonical fixture"
        )
        assert audit_score >= 0.7, (
            f"audit_score collapsed to {audit_score} for the canonical fixture; "
            "expected >= 0.7"
        )

    def test_fixture_uses_findings_key_not_issues(self, audit_fixture):
        """Fixture must use 'findings', not the legacy 'issues' key."""
        assert "findings" in audit_fixture, "fixture missing 'findings' key"
        assert "issues" not in audit_fixture, "fixture must not use legacy 'issues' key"

    def test_fixture_uses_severity_field_not_level(self, audit_fixture):
        """Each finding must use 'severity', not the legacy 'level' field."""
        for finding in audit_fixture["findings"]:
            assert "severity" in finding, f"finding missing 'severity' field: {finding}"
            assert "level" not in finding, (
                f"finding must not use legacy 'level' field: {finding}"
            )


class TestReleaseAuditChallengeFixtureShape:
    def test_challenge_sample_is_recognized_by_backend_synthesis(self, audit_fixture):
        assert (
            ArtifactType.RELEASE_AUDIT_CHALLENGE_REPORT.value
            == "RELEASE_AUDIT_CHALLENGE_REPORT"
        )
        for key in (
            "agreement",
            "agreement_assessment",
            "missing_findings",
            "overstated_findings",
            "challenge_severity",
        ):
            assert key in _CHALLENGE_SAMPLE
        assert "issues" not in _CHALLENGE_SAMPLE
        assert "level" not in _CHALLENGE_SAMPLE["missing_findings"][0]

        out = _compute_audit_challenge_synthesis(audit_fixture, _CHALLENGE_SAMPLE)

        assert out["status"] == "available"
        assert out["reported_agreement"] == "partial"
        assert out["challenge_severity"] == "medium"
        assert out["counts"]["challenger_missing"] == 1
        assert out["counts"]["challenger_overstated"] == 1
        assert out["disagreement"]["challenger_only"][0]["summary"] == (
            "Missed release-risk finding."
        )
        assert out["disagreement"]["overstated_findings"][0]["summary"] == (
            "Primary overstates migration risk."
        )


class TestAuditsTsSchemaContract:
    """Introspect agent/src/handlers/audits.ts to ensure the release-audit prompt
    documents 'findings' and 'severity' in its JSON schema block, not legacy
    'issues'/'level'.  (The release-audit handler moved here from specialized.ts
    when the agent handlers were decomposed into focused modules in #6597.)
    """

    @pytest.fixture(scope="class")
    def ts_source(self) -> str:
        path = _resolve_audits_ts_path()
        if path is None:
            pytest.skip(
                "agent/src/handlers/audits.ts not available in this test "
                "environment (CI docker context builds from ./backend; run via "
                "scripts/test-backend.sh from a checkout for enforcement)"
            )
        return pathlib.Path(path).read_text()

    def test_audits_ts_uses_findings_not_issues(self, ts_source):
        """The agent prompt must reference 'findings' as the top-level key."""
        # The JSON schema block emitted to the LLM must instruct it to use "findings".
        assert '"findings"' in ts_source, (
            "agent/src/handlers/audits.ts does not contain '\"findings\"'; "
            "the prompt may have drifted to a legacy key name"
        )

    def test_audits_ts_uses_severity_not_level_in_schema(self, ts_source):
        """The agent prompt's findings schema must use 'severity', not 'level'."""
        assert '"severity"' in ts_source, (
            "agent/src/handlers/audits.ts does not contain '\"severity\"'; "
            "the prompt may have drifted to a legacy field name"
        )

    def _extract_audit_schema_block(self, ts_source: str) -> str:
        """Extract the fenced JSON block documenting the release audit output schema.

        The block is inside a TypeScript template literal, so backtick fence
        markers appear escaped as ``\\`\\`\\`json`` in the raw source text.
        """
        import re

        # Inside a TS template literal, ``` is written as \`\`\`.
        blocks = re.findall(r"\\`\\`\\`json\s*(.*?)\\`\\`\\`", ts_source, re.DOTALL)
        audit_blocks = [b for b in blocks if "audit_score" in b]
        assert audit_blocks, (
            "Could not find a fenced JSON block containing 'audit_score' in "
            "agent/src/handlers/audits.ts — schema block may have been removed"
        )
        return audit_blocks[0]

    def test_audits_ts_does_not_instruct_issues_key(self, ts_source):
        """The agent prompt's documented JSON example must not use 'issues' as the
        findings key — that is the legacy shape the backend falls back to but does
        not prefer.  If the prompt documents 'issues', newly emitted artifacts will
        use it and the primary parse path will silently fall back, masking drift.
        """
        schema_block = self._extract_audit_schema_block(ts_source)
        assert '"issues"' not in schema_block, (
            "The JSON schema block in audits.ts documents 'issues' as the "
            "findings key — must use 'findings' to avoid drift"
        )

    def test_audits_ts_does_not_use_level_in_findings_schema(self, ts_source):
        """The agent prompt's finding entry schema must not list 'level' as the
        severity field name — that is a legacy alias the backend tolerates but the
        primary parse path uses 'severity'.
        """
        schema_block = self._extract_audit_schema_block(ts_source)
        assert '"level"' not in schema_block, (
            "The JSON schema block in audits.ts uses 'level' as a severity "
            "field name — must use 'severity' to avoid drift"
        )

    def _extract_challenge_schema_block(self, ts_source: str) -> str:
        import re

        blocks = re.findall(r"\\`\\`\\`json\s*(.*?)\\`\\`\\`", ts_source, re.DOTALL)
        challenge_blocks = [b for b in blocks if "challenge_severity" in b]
        assert challenge_blocks, (
            "Could not find a fenced JSON block containing 'challenge_severity' "
            "in agent/src/handlers/audits.ts"
        )
        return challenge_blocks[0]

    def test_audits_ts_challenge_schema_uses_backend_contract_keys(self, ts_source):
        assert "RELEASE_AUDIT_CHALLENGE_REPORT" in ts_source
        schema_block = self._extract_challenge_schema_block(ts_source)
        for key in (
            "agreement",
            "agreement_assessment",
            "missing_findings",
            "overstated_findings",
            "challenge_severity",
            "severity",
        ):
            assert f'"{key}"' in schema_block
        assert '"issues"' not in schema_block
        assert '"level"' not in schema_block
