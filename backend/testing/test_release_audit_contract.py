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


def _resolve_merge_grade_ts_path() -> "str | None":
    """Locate ``agent/src/handlers/merge-grade.ts`` across local + Docker envs.

    Mirrors ``_resolve_audits_ts_path``: CI runs pytest inside the backend Docker
    image (built from ./backend only), so the agent tree is not on the filesystem
    unless ``scripts/test-backend.sh`` mounts it at ``/agent/src``.  Returns the
    first existing path or ``None`` to let the test skip rather than error.
    """
    env_override = os.getenv("AGENT_MERGE_GRADE_TS")
    candidates = []
    if env_override:
        candidates.append(env_override)
    candidates.append("/agent/src/handlers/merge-grade.ts")
    candidates.append(
        os.path.normpath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "..",
                "agent",
                "src",
                "handlers",
                "merge-grade.ts",
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


class TestDeterministicCoDeleteClassification:
    """_score_audit prefers computed_test_erosion.deleted_test_file_classifications
    over the LLM's deleted_test_file_classifications for co-deleted test files."""

    def _make_report(
        self,
        deleted_test_files: list,
        computed_classifications: list | None = None,
        llm_classifications: list | None = None,
    ) -> dict:
        report: dict = {
            "audit_score": 1.0,
            "findings": [],
            "computed_test_erosion": {
                "deleted_test_files": deleted_test_files,
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            "test_erosion": {
                "deleted_test_files": deleted_test_files,
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
        }
        if computed_classifications is not None:
            report["computed_test_erosion"]["deleted_test_file_classifications"] = computed_classifications
        if llm_classifications is not None:
            report["deleted_test_file_classifications"] = llm_classifications
        return report

    def test_deterministic_dead_code_removal_passes_without_llm_tag(self):
        """A file classified dead_code_removal in computed_test_erosion passes
        even when the LLM emits no deleted_test_file_classifications at all."""
        report = self._make_report(
            deleted_test_files=["admin-ui/src/components/Foo.test.tsx"],
            computed_classifications=[
                {
                    "path": "admin-ui/src/components/Foo.test.tsx",
                    "classification": "dead_code_removal",
                    "rationale": "Subject Foo.tsx is also a D-line in git diff --name-status.",
                }
            ],
            llm_classifications=None,  # LLM produced no classifications
        )
        _, test_erosion = _score_audit(report)
        assert test_erosion is not None, (
            "Expected a numeric test_erosion score when the deterministic "
            "classification clears the file as dead_code_removal; got None (hard-block)."
        )

    def test_unflagged_deletion_passes_but_genuine_erosion_blocks(self):
        """Fail-open disposition (operator steer, #11722): a deleted test file with
        NO classification from either the deterministic or LLM path PASSES — only an
        explicit ``genuine_erosion`` tag hard-blocks.

        This was previously asserted fail-closed (absent → block) but #11722 flipped
        the default disposition to fail-open so the auditor's frequent failure to
        emit a tag stops false-blocking legitimate co-deletion/declutter cuts. The
        deterministic-detection-and-overlay path added in #11721 is unchanged; only
        the default for an un-flagged deletion is allow. The narrow erosion case
        (subject demonstrably present, test stripped) still blocks — but only when
        the auditor affirmatively classifies it ``genuine_erosion``.
        """
        # Part 1: no deterministic and no LLM classification → fail-OPEN (passes).
        report_unflagged = self._make_report(
            deleted_test_files=["admin-ui/src/components/Foo.test.tsx"],
            computed_classifications=None,  # no deterministic classification
            llm_classifications=None,  # LLM also produced nothing
        )
        _, test_erosion = _score_audit(report_unflagged)
        assert test_erosion is not None, (
            "Expected a non-None erosion score (fail-open) when the deleted test "
            "file carries no classification — only explicit genuine_erosion blocks."
        )

        # Part 2: explicit genuine_erosion (subject still present) → still hard-blocks.
        report_erosion = self._make_report(
            deleted_test_files=["admin-ui/src/components/Live.test.tsx"],
            computed_classifications=None,  # deterministic path never emits genuine_erosion
            llm_classifications=[
                {
                    "path": "admin-ui/src/components/Live.test.tsx",
                    "classification": "genuine_erosion",
                    "rationale": "Subject Live.tsx is present and unchanged.",
                }
            ],
        )
        _, erosion_genuine = _score_audit(report_erosion)
        assert erosion_genuine is None, (
            "Expected None (hard-block) for an explicit genuine_erosion "
            "classification — the one case fail-open preserves."
        )

    def test_deterministic_overrides_absent_llm_for_multiple_files(self):
        """Multiple files all cleared deterministically all pass, even with no LLM tags."""
        report = self._make_report(
            deleted_test_files=[
                "admin-ui/src/components/Foo.test.tsx",
                "admin-ui/src/components/Bar.viewport.test.tsx",
            ],
            computed_classifications=[
                {
                    "path": "admin-ui/src/components/Foo.test.tsx",
                    "classification": "dead_code_removal",
                    "rationale": "Subject Foo.tsx deleted.",
                },
                {
                    "path": "admin-ui/src/components/Bar.viewport.test.tsx",
                    "classification": "dead_code_removal",
                    "rationale": "Subject Bar.tsx deleted.",
                },
            ],
            llm_classifications=None,
        )
        _, test_erosion = _score_audit(report)
        assert test_erosion is not None, (
            "Expected numeric test_erosion when all deleted files cleared deterministically."
        )

    def test_partial_deterministic_coverage_passes_unflagged_blocks_genuine_erosion(self):
        """Partial deterministic coverage under the fail-open disposition (#11722):
        one file cleared deterministically as ``dead_code_removal``, a second file
        left unflagged.

        Fail-open: the unflagged file PASSES (no longer hard-blocks merely because
        the auditor didn't tag it). A second file is hard-blocked ONLY when it
        carries an explicit ``genuine_erosion`` classification.
        """
        # Part 1: one file cleared deterministically, second unflagged → passes.
        report_unflagged = self._make_report(
            deleted_test_files=[
                "admin-ui/src/components/Foo.test.tsx",
                "admin-ui/src/components/Live.test.tsx",  # unflagged
            ],
            computed_classifications=[
                {
                    "path": "admin-ui/src/components/Foo.test.tsx",
                    "classification": "dead_code_removal",
                    "rationale": "Subject Foo.tsx deleted.",
                },
                # Live.test.tsx NOT classified — unflagged passes under fail-open.
            ],
            llm_classifications=None,
        )
        _, test_erosion = _score_audit(report_unflagged)
        assert test_erosion is not None, (
            "Expected non-None erosion (fail-open): one file is cleared "
            "deterministically and the second is unflagged, which now passes."
        )

        # Part 2: second file flagged genuine_erosion (subject present) → blocks.
        report_genuine = self._make_report(
            deleted_test_files=[
                "admin-ui/src/components/Foo.test.tsx",
                "admin-ui/src/components/Live.test.tsx",  # subject still present
            ],
            computed_classifications=[
                {
                    "path": "admin-ui/src/components/Foo.test.tsx",
                    "classification": "dead_code_removal",
                    "rationale": "Subject Foo.tsx deleted.",
                },
            ],
            llm_classifications=[
                {
                    "path": "admin-ui/src/components/Live.test.tsx",
                    "classification": "genuine_erosion",
                    "rationale": "Subject Live.tsx is present and unchanged.",
                }
            ],
        )
        _, erosion_genuine = _score_audit(report_genuine)
        assert erosion_genuine is None, (
            "Expected None (hard-block) when a deleted file is explicitly "
            "classified genuine_erosion, even alongside a deterministically cleared file."
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

def _resolve_audit_challenge_ts_path() -> "str | None":
    """Locate ``agent/src/handlers/audit-challenge.ts`` across local + Docker.

    Mirrors :func:`_resolve_audits_ts_path`.  The binding release-audit
    challenge handler emits a RELEASE_AUDIT_CHALLENGE artifact whose shape a
    backend consumer reads verbatim; these tests pin that the producer's prompt
    still documents the contract keys + verdict enum.
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

class TestAuditChallengeTsContract:
    """Introspect agent/src/handlers/audit-challenge.ts to pin the binding
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
async def test_spawn_release_audit_challenge_defaults_to_opus_quality_tier(db_session):
    """``_spawn_release_audit_challenge_task`` MUST explicitly set quality_tier
    to Opus in the spawned task's payload — regression guard against a
    cost-saving edit silently downgrading this binding adversarial
    second-opinion gate below Opus."""
    from app import release_qualifier as rq
    from app.models import Project

    project = Project(
        name="RQ-Challenge-Tier",
        user_id="rq-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "cafe0011" * 5  # 40-char placeholder SHA
    attempt_id = uuid.uuid4()
    task = await rq._spawn_release_audit_challenge_task(
        db_session,
        project.id,
        version_id=uuid.uuid4(),
        base_branch="main",
        diff_range=f"main...{candidate}",
        candidate_sha=candidate,
        qualification_attempt_id=attempt_id,
    )
    await db_session.commit()
    await db_session.refresh(task)

    assert (task.first_payload or {}).get("quality_tier") == "opus", (
        "Spawned release_audit_challenge must explicitly carry quality_tier='opus' "
        "— regression guard against silent downgrade"
    )
    # Provenance: qualification_attempt_id must propagate through payload + context.
    assert task.first_payload.get("qualification_attempt_id") == str(attempt_id), (
        "qualification_attempt_id must be propagated in the payload"
    )
    assert task.context.get("qualification_attempt_id") == str(attempt_id), (
        "qualification_attempt_id must be propagated in the context"
    )


@pytest.mark.asyncio
async def test_spawn_release_audit_task_defaults_to_opus_quality_tier(db_session):
    """``_spawn_release_audit_task`` MUST explicitly set quality_tier to Opus
    in the spawned task's payload — regression guard against a cost-saving
    edit silently downgrading the primary release-audit judgment gate below
    Opus."""
    from app import release_qualifier as rq
    from app.models import Project

    project = Project(
        name="RQ-Audit-Tier",
        user_id="rq-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "dead1234" * 5  # 40-char placeholder SHA
    attempt_id = uuid.uuid4()
    task = await rq._spawn_release_audit_task(
        db_session,
        project.id,
        version_id=uuid.uuid4(),
        base_branch="main",
        diff_range=f"main...{candidate}",
        candidate_sha=candidate,
        qualification_attempt_id=attempt_id,
    )
    await db_session.commit()
    await db_session.refresh(task)

    assert (task.first_payload or {}).get("quality_tier") == "opus", (
        "Spawned release_audit must explicitly carry quality_tier='opus' "
        "— regression guard against silent downgrade"
    )
    # Provenance: qualification_attempt_id must propagate through payload + context.
    assert task.first_payload.get("qualification_attempt_id") == str(attempt_id), (
        "qualification_attempt_id must be propagated in the payload"
    )
    assert task.context.get("qualification_attempt_id") == str(attempt_id), (
        "qualification_attempt_id must be propagated in the context"
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
    attempt_id = uuid.uuid4()
    task = await rq._spawn_release_audit_challenge_task(
        db_session,
        project.id,
        version_id=version_id,
        base_branch="main",
        diff_range=f"main...{candidate}",
        candidate_sha=candidate,
        qualification_attempt_id=attempt_id,
    )
    await db_session.commit()
    await db_session.refresh(task)

    assert task.first_payload["type"] == "release_audit_challenge"
    assert task.first_payload["candidate_sha"] == candidate
    assert task.first_payload["base_branch"] == "main"
    assert task.first_payload["diff_range"] == f"main...{candidate}"
    assert task.first_payload["qualification_attempt_id"] == str(attempt_id)
    # Context is scoped so the lookup helper can find it by candidate + version.
    assert task.context["candidate_sha"] == candidate
    assert task.context["release_version_id"] == str(version_id)
    assert task.context["qualification_attempt_id"] == str(attempt_id)


@pytest.mark.asyncio
async def test_get_latest_release_audit_challenge_scoped_by_sha(db_session):
    """``get_latest_release_audit_challenge`` returns the parsed binding verdict
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
        qualification_attempt_id=uuid.uuid4(),
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


@pytest.mark.asyncio
async def test_release_audit_resolves_to_opus_end_to_end(db_session):
    """Spawned release_audit gates resolve to Opus tier end-to-end via
    resolve_task_quality_tier, confirming the explicit quality_tier='opus'
    payload value is honored."""
    from app import release_qualifier as rq
    from app.max_plan_pool import resolve_task_quality_tier
    from app.models import Project

    project = Project(
        name="RQ-Audit-Resolve",
        user_id="rq-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "aabbccdd" * 5  # 40-char placeholder SHA
    attempt_id = uuid.uuid4()
    task = await rq._spawn_release_audit_task(
        db_session,
        project.id,
        version_id=uuid.uuid4(),
        base_branch="main",
        diff_range=f"main...{candidate}",
        candidate_sha=candidate,
        qualification_attempt_id=attempt_id,
    )
    await db_session.commit()
    await db_session.refresh(task)

    resolved_tier = resolve_task_quality_tier(task, {})
    assert resolved_tier == "opus", (
        f"release_audit must resolve to Opus tier end-to-end, got {resolved_tier!r}"
    )


@pytest.mark.asyncio
async def test_release_audit_challenge_resolves_to_opus_end_to_end(db_session):
    """Spawned release_audit_challenge gates resolve to Opus tier end-to-end via
    resolve_task_quality_tier, confirming the explicit quality_tier='opus'
    payload value is honored."""
    from app import release_qualifier as rq
    from app.max_plan_pool import resolve_task_quality_tier
    from app.models import Project

    project = Project(
        name="RQ-Challenge-Resolve",
        user_id="rq-user",
        github_owner="acme",
        github_repo="widget",
        integration_branch="automain",
        promotion_target="main",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    candidate = "eeff0011" * 5  # 40-char placeholder SHA
    attempt_id = uuid.uuid4()
    task = await rq._spawn_release_audit_challenge_task(
        db_session,
        project.id,
        version_id=uuid.uuid4(),
        base_branch="main",
        diff_range=f"main...{candidate}",
        candidate_sha=candidate,
        qualification_attempt_id=attempt_id,
    )
    await db_session.commit()
    await db_session.refresh(task)

    resolved_tier = resolve_task_quality_tier(task, {})
    assert resolved_tier == "opus", (
        f"release_audit_challenge must resolve to Opus tier end-to-end, got {resolved_tier!r}"
    )


def test_default_quality_tier_is_opus():
    """DEFAULT_QUALITY_TIER must be Opus — regression guard against a
    cost-saving edit downgrading all quality_tier fallbacks."""
    from app.max_plan_pool import DEFAULT_QUALITY_TIER

    assert DEFAULT_QUALITY_TIER == "opus", (
        f"DEFAULT_QUALITY_TIER downgrade detected: {DEFAULT_QUALITY_TIER!r} "
        "— release gates rely on opus-tier fallback; "
        "explicit quality_tier='opus' payloads will be overridden if default changes"
    )


# ===========================================================================
# blocked_by triage taxonomy (design edcc1e6a §3.4) — merge-grade contract
# ===========================================================================


class TestMergeGradeBlockedByTaxonomy:
    """Pin the canonical blocked_by triage taxonomy on the backend side.

    The merge-grade contract (design edcc1e6a §3.4) lets a residual say WHY it
    is unmet: ``unbuilt`` | ``out_of_scope`` | ``blocked_external``.  Only
    ``unbuilt`` may consume campaign rung budget.  These tests pin the canonical
    values, the defaulting rule (an unclassified non-met residual defaults to
    ``unbuilt`` so it never silently leaves the budget), and the budget rule.
    """

    def test_blocked_by_values_are_exactly_the_three_triage_values(self):
        from app.release_scoring import _BLOCKED_BY_VALUES

        assert frozenset(
            {"unbuilt", "out_of_scope", "blocked_external"}
        ) == _BLOCKED_BY_VALUES, (
            "The canonical blocked_by triage taxonomy drifted — expected exactly "
            f"{{'unbuilt', 'out_of_scope', 'blocked_external'}}, got {_BLOCKED_BY_VALUES}"
        )

    def test_release_qualifier_audit_re_exports_the_canonical_taxonomy(self):
        """release_qualifier_audit must re-export the canonical values so the
        merge-grade contract's two sides cannot drift apart."""
        from app import release_qualifier_audit as rqa
        from app import release_scoring as rs

        assert rqa.BLOCKED_BY_VALUES == rs._BLOCKED_BY_VALUES
        assert rqa.DEFAULT_BLOCKED_BY == rs._DEFAULT_BLOCKED_BY
        assert rqa.normalize_blocked_by is rs.normalize_blocked_by
        assert rqa.blocked_by_consumes_rung_budget is rs.blocked_by_consumes_rung_budget

    def test_normalize_blocked_by_met_returns_none(self):
        from app.release_scoring import normalize_blocked_by

        assert normalize_blocked_by("unbuilt", "met") is None
        assert normalize_blocked_by(None, "met") is None
        assert normalize_blocked_by("out_of_scope", "met") is None

    def test_normalize_blocked_by_preserves_all_three_triage_values(self):
        from app.release_scoring import normalize_blocked_by

        for value in ("unbuilt", "out_of_scope", "blocked_external"):
            assert normalize_blocked_by(value, "not_met") == value, value
            assert normalize_blocked_by(value, "partial") == value, value

    def test_normalize_blocked_by_defaults_absent_or_invalid_to_unbuilt(self):
        from app.release_scoring import normalize_blocked_by

        for raw in (None, "", "  ", "unknown", 42, {"a": 1}):
            assert normalize_blocked_by(raw, "not_met") == "unbuilt", (
                f"absent/invalid blocked_by {raw!r} must default to unbuilt, not "
                "silently become out_of_scope (which would drop the residual from "
                "the campaign rung budget)"
            )

    def test_only_unbuilt_consumes_rung_budget(self):
        from app.release_scoring import blocked_by_consumes_rung_budget

        # Only unbuilt (and the absent default, which normalizes to unbuilt)
        # consumes campaign rung budget.
        assert blocked_by_consumes_rung_budget("unbuilt") is True
        assert blocked_by_consumes_rung_budget(None) is True
        # The two terminal triage values must NOT consume budget.
        assert blocked_by_consumes_rung_budget("out_of_scope") is False
        assert blocked_by_consumes_rung_budget("blocked_external") is False


class TestMergeGradeBlockedByRealVerdict:
    """Drive a REAL merge-test verdict through ``evaluate_merge_grade`` and assert
    the normalized per-category verdicts carry the blocked_by triage.

    This is the acceptance-evidence path: the test produces an actual merge-grade
    verdict (pure, no DB) from an artifact whose category verdicts use each triage
    value, then asserts the normalized output preserves the triage and that only
    ``unbuilt`` residuals consume the campaign rung budget.
    """

    @staticmethod
    def _verdict_artifact() -> dict:
        return {
            "graded_candidate_sha": "c0ffee" * 5,
            "covers_goal": False,
            "coverage_score": 0.3,
            "biggest_gap": "The goal requires X, but the merged diff only adds Y.",
            "category_verdicts": [
                {
                    "category_key": "c-1",
                    "status": "met",
                    "blocked_by": None,
                    "gap": None,
                    "citations": [{"kind": "diff", "ref": "backend/app/foo.py:12"}],
                },
                {
                    "category_key": "c-2",
                    "status": "not_met",
                    "blocked_by": "unbuilt",
                    "gap": "Capability Z is not wired up.",
                    "citations": [{"kind": "diff", "ref": "backend/app/bar.py"}],
                },
                {
                    "category_key": "c-3",
                    "status": "not_met",
                    "blocked_by": "out_of_scope",
                    "gap": "Deliberately deferred to a later version.",
                    "citations": [{"kind": "diff", "ref": "backend/app/baz.py"}],
                },
                {
                    "category_key": "c-4",
                    "status": "partial",
                    "blocked_by": "blocked_external",
                    "gap": "Gated on an upstream deploy.",
                    "citations": [{"kind": "diff", "ref": "backend/app/qux.py"}],
                },
            ],
            "residual_uncaptured_gap": None,
        }

    @staticmethod
    def _active_categories() -> list[dict]:
        return [
            {"category_key": "c-1", "text": "First"},
            {"category_key": "c-2", "text": "Second"},
            {"category_key": "c-3", "text": "Third"},
            {"category_key": "c-4", "text": "Fourth"},
            {"category_key": "c-5", "text": "Never graded"},
        ]

    def test_evaluate_merge_grade_preserves_triage_on_each_verdict(self):
        from app.release_scoring import evaluate_merge_grade

        result = evaluate_merge_grade(
            "Deliver X, Y, and Z.",
            self._verdict_artifact(),
            active_categories=self._active_categories(),
        )
        assert result["status"] == "fail"
        assert result["blocked"] is True
        verdicts = {
            v["category_key"]: v
            for v in result["category_verdicts"]
            if isinstance(v, dict)
        }
        assert verdicts["c-1"]["status"] == "met"
        assert verdicts["c-1"]["blocked_by"] is None
        assert verdicts["c-2"]["blocked_by"] == "unbuilt"
        assert verdicts["c-3"]["blocked_by"] == "out_of_scope"
        assert verdicts["c-4"]["blocked_by"] == "blocked_external"

    def test_never_graded_category_defaults_to_unbuilt(self):
        from app.release_scoring import evaluate_merge_grade

        result = evaluate_merge_grade(
            "Deliver X, Y, and Z.",
            self._verdict_artifact(),
            active_categories=self._active_categories(),
        )
        never_graded = next(
            v
            for v in result["category_verdicts"]
            if v["category_key"] == "c-5"
        )
        assert never_graded["status"] == "not_met"
        assert never_graded["blocked_by"] == "unbuilt", (
            "A category the grader never assessed is 'not_met' with blocked_by "
            "defaulting to unbuilt — the capability was never built."
        )

    def test_only_unbuilt_verdicts_consume_rung_budget_from_a_real_verdict(self):
        from app.release_scoring import (
            blocked_by_consumes_rung_budget,
            evaluate_merge_grade,
        )

        result = evaluate_merge_grade(
            "Deliver X, Y, and Z.",
            self._verdict_artifact(),
            active_categories=self._active_categories(),
        )
        consumers = [
            v["category_key"]
            for v in result["category_verdicts"]
            if v["status"] != "met" and blocked_by_consumes_rung_budget(v.get("blocked_by"))
        ]
        # c-2 is unbuilt; c-5 was never graded (→ unbuilt).  c-3 (out_of_scope)
        # and c-4 (blocked_external) are terminal and must NOT consume budget.
        assert consumers == ["c-2", "c-5"], (
            f"only unbuilt residuals may consume campaign rung budget, got {consumers}"
        )

    def test_unmet_by_triage_sub_score_buckets_are_correct(self):
        from app.release_scoring import compute_composite
        from app.release_scoring import evaluate_merge_grade

        grade = evaluate_merge_grade(
            "Deliver X, Y, and Z.",
            self._verdict_artifact(),
            active_categories=self._active_categories(),
        )
        composite = compute_composite(
            ci_green=True,
            audit_score=0.9,
            test_erosion=0.0,
            smoketest_score=0.9,
            policy={},
            category_verdicts=grade["category_verdicts"],
            residual_uncaptured_gap=grade["residual_uncaptured_gap"],
            merge_grade_misses=True,
            merge_grade_exempt=False,
        )
        sub_scores = composite["sub_scores"]
        assert sub_scores.get("unmet_by_triage") == {
            "unbuilt": 2,
            "out_of_scope": 1,
            "blocked_external": 1,
        }, (
            "unmet_by_triage must bucket the UNMET categories by their blocked_by "
            f"triage, got {sub_scores.get('unmet_by_triage')}"
        )


class TestMergeGradeTsBlockedByContract:
    """Introspect agent/src/handlers/merge-grade.ts to ensure the agent side of
    the merge-grade contract still documents the blocked_by triage taxonomy and
    the default-unbuilt / only-unbuilt-consumes-budget rules."""

    @pytest.fixture(scope="class")
    def ts_source(self) -> str:
        path = _resolve_merge_grade_ts_path()
        if path is None:
            pytest.skip(
                "agent/src/handlers/merge-grade.ts not available in this test "
                "environment (CI docker context builds from ./backend; run via "
                "scripts/test-backend.sh from a checkout for enforcement)"
            )
        return pathlib.Path(path).read_text()

    def test_documents_all_three_blocked_by_values(self, ts_source):
        for value in ("unbuilt", "out_of_scope", "blocked_external"):
            assert repr(value) in ts_source or f"'{value}'" in ts_source, (
                f"agent/src/handlers/merge-grade.ts does not reference "
                f"blocked_by value {value!r} — the triage taxonomy drifted"
            )

    def test_defaults_to_unbuilt(self, ts_source):
        assert "DEFAULT_BLOCKED_BY: BlockedBy = 'unbuilt'" in ts_source, (
            "agent/src/handlers/merge-grade.ts must default an unclassified "
            "non-met residual to 'unbuilt' (never silently out_of_scope)"
        )

    def test_only_unbuilt_consumes_rung_budget_in_ts(self, ts_source):
        assert "blockedBy === 'unbuilt'" in ts_source, (
            "blockedByConsumesRungBudget must consume budget only for 'unbuilt'"
        )
        assert "blockedBy == null" in ts_source, (
            "the absent/NULL blocked_by default must also consume budget"
        )

    def test_category_verdict_interface_carries_blocked_by(self, ts_source):
        assert "blocked_by: BlockedBy | null;" in ts_source, (
            "agent/src/handlers/merge-grade.ts CategoryVerdict interface must "
            "declare 'blocked_by: BlockedBy | null'"
        )

    def test_prompt_documents_blocked_by_semantics(self, ts_source):
        assert '"blocked_external"' in ts_source, (
            "the merge-grade agent prompt must teach the grader the "
            "blocked_external triage value"
        )
        # The prompt lives inside a TS template literal, so backticks appear
        # escaped as `\`` in the raw source — match them with a raw string.
        assert r"Only \`unbuilt\` residuals commission follow-up build work" in ts_source, (
            "the merge-grade agent prompt must state the budget rule (only unbuilt "
            "commissions follow-up build work)"
        )
