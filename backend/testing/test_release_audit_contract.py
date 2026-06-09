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
import uuid

import pytest

# Pure functions — no DB, no I/O.
from app.release_scoring import (
    _audit_missing_reason,
    _score_audit,
)

_FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "release_audit_sample.json"


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


def _resolve_audits_ts_path() -> "str | None":
    """Locate ``agent/src/handlers/audits.ts`` across local + Docker envs.

    The release-audit prompt and its JSON schema block live in ``audits.ts``
    (moved out of ``specialized.ts`` when the agent handlers were decomposed
    into focused modules in #6597).  CI runs pytest inside the backend Docker
    image (built from ./backend only), so the agent tree is not on the
    filesystem unless ``scripts/test-backend.sh`` mounts it at ``/agent/src``.
    Returns the first existing path or ``None`` to let the test skip rather
    than error.
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


def _resolve_audit_challenge_ts_path() -> "str | None":
    """Locate ``agent/src/handlers/audit-challenge.ts`` across local + Docker.

    The advisory release-audit challenge handler emits a
    RELEASE_AUDIT_CHALLENGE artifact whose shape a backend consumer reads
    verbatim; these tests pin that the producer's prompt still documents the
    contract keys + verdict enum.
    """
    env_override = os.getenv("AGENT_AUDIT_CHALLENGE_TS")
    candidates = []
    if env_override:
        candidates.append(env_override)
    candidates.append("/agent/src/handlers/audit-challenge.ts")
    candidates.append(
        os.path.normpath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "..",
                "agent",
                "src",
                "handlers",
                "audit-challenge.ts",
            )
        )
    )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


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


class TestAuditChallengeTsContract:
    """Introspect agent/src/handlers/audit-challenge.ts to pin the advisory
    RELEASE_AUDIT_CHALLENGE artifact contract the backend consumer relies on.
    """

    @pytest.fixture(scope="class")
    def ts_source(self) -> str:
        path = _resolve_audit_challenge_ts_path()
        if path is None:
            pytest.skip(
                "agent/src/handlers/audit-challenge.ts not available in this "
                "test environment (CI docker context builds from ./backend; run "
                "via scripts/test-backend.sh from a checkout for enforcement)"
            )
        return pathlib.Path(path).read_text()

    def test_documents_top_level_contract_keys(self, ts_source):
        assert '"challenged_candidate_sha"' in ts_source, (
            "audit-challenge.ts must document the 'challenged_candidate_sha' key"
        )
        assert '"verdicts"' in ts_source, (
            "audit-challenge.ts must document the 'verdicts' key"
        )

    def test_documents_full_verdict_enum(self, ts_source):
        for verdict in ("confirmed", "refuted_false_positive", "uncertain"):
            assert verdict in ts_source, (
                f"audit-challenge.ts must document the '{verdict}' verdict value"
            )


@pytest.mark.asyncio
async def test_spawn_release_audit_challenge_pins_candidate(db_session):
    """``_spawn_release_audit_challenge_task`` pins the challenge child to the
    exact candidate SHA + diff range, mirroring the release_audit spawn."""
    from app import release_qualifier as rq
    from app.models import Project

    project = Project(
        name="RQ-Challenge",
        user_id="rq-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "abcdef12" * 5  # 40-char placeholder SHA
    version_id = uuid.uuid4()
    task = await rq._spawn_release_audit_challenge_task(
        db_session,
        project.id,
        version_id=version_id,
        base_branch="main",
        diff_range=f"main...{candidate}",
        candidate_sha=candidate,
    )
    await db_session.commit()
    await db_session.refresh(task)

    assert task.first_payload["type"] == "release_audit_challenge"
    assert task.first_payload["candidate_sha"] == candidate
    assert task.first_payload["base_branch"] == "main"
    assert task.first_payload["diff_range"] == f"main...{candidate}"
    # Context is scoped so the lookup helper can find it by candidate + version.
    assert task.context["candidate_sha"] == candidate
    assert task.context["release_version_id"] == str(version_id)


@pytest.mark.asyncio
async def test_get_latest_release_audit_challenge_scoped_by_sha(db_session):
    """``get_latest_release_audit_challenge`` returns the parsed advisory verdict
    for the matching candidate, and ``None`` for a non-matching candidate."""
    from app import release_qualifier as rq
    from app.models import Artifact, ArtifactType, Project

    project = Project(
        name="RQ-Challenge-Lookup",
        user_id="rq-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "feedbeef" * 5  # 40-char placeholder SHA
    challenge_task = await rq._spawn_release_audit_challenge_task(
        db_session,
        project.id,
        version_id=uuid.uuid4(),
        base_branch="main",
        diff_range=f"main...{candidate}",
        candidate_sha=candidate,
    )
    await db_session.commit()

    verdict_blob = {
        "challenged_candidate_sha": candidate,
        "verdicts": [
            {
                "severity": "high",
                "summary": "sql injection in auth",
                "file": "app/auth.py",
                "verdict": "refuted_false_positive",
                "reason": "Read app/auth.py:60 — the value is a hardcoded "
                "constant, not user input, so the injection finding is a "
                "false positive.",
            }
        ],
    }
    db_session.add(
        Artifact(
            task_id=challenge_task.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_CHALLENGE,
            logs=json.dumps(verdict_blob),
        )
    )
    await db_session.commit()

    found = await rq.get_latest_release_audit_challenge(
        db_session, project.id, candidate_sha=candidate
    )
    assert found is not None
    assert found["candidate_sha"] == candidate
    assert found["challenge_task_id"] == str(challenge_task.id)
    assert found["verdict"] == verdict_blob
    assert found["verdict"]["verdicts"][0]["verdict"] == "refuted_false_positive"

    other = await rq.get_latest_release_audit_challenge(
        db_session, project.id, candidate_sha="0" * 40
    )
    assert other is None
    assert (
        await rq.get_latest_release_audit_challenge(
            db_session, project.id, candidate_sha=None
        )
        is None
    )
