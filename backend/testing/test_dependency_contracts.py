"""Characterization tests for dependency-sensitive contracts.

These tests pin observable behavior at the seams most likely to silently
regress from dependency bumps:

  1. GitHub App JWT claim shape (github_auth.generate_github_app_jwt):
     PyJWT ≥2.10 tightened issuer claim handling — ``iss`` must decode as a
     string (not int), ``iat`` must sit 60 s in the past (clock-drift buffer),
     ``exp`` must be ≈10 min ahead.  A bump that changes how PyJWT encodes or
     validates integer/string claims would silently break GitHub App auth.

  2. Pydantic model_dump byte-size determinism (payload_schemas):
     The ``exclude_none=True`` serialization path gates agent-startup budget and
     DB-constraint compliance (chk_task_payloads_size).  A pydantic bump that
     changes field exclusion semantics (e.g. the 2.13 after-validator or
     extra-field tracking changes) would silently bloat or shrink payloads.

Both suites run without a database and without real GitHub credentials.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


# ---------------------------------------------------------------------------
# Shared RSA test key — generated once per session, never hits network
# ---------------------------------------------------------------------------

def _generate_test_rsa_key():
    """Return a (private_key_pem, public_key_pem) pair for test-only use."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


_TEST_PRIVATE_PEM, _TEST_PUBLIC_PEM = _generate_test_rsa_key()
_TEST_APP_ID = "12345"


# ---------------------------------------------------------------------------
# 1. GitHub App JWT claim shape
# ---------------------------------------------------------------------------


class TestGitHubAppJWTClaimShape:
    """generate_github_app_jwt must produce a decodable RS256 JWT with the exact
    claim shape GitHub's API expects.  These tests fail if a PyJWT bump alters
    how string/integer issuer claims are encoded or if the iat/exp offsets shift.
    """

    def _call(self):
        """Invoke generate_github_app_jwt with patched env vars."""
        with (
            patch("app.github_auth.GITHUB_APP_ID", _TEST_APP_ID),
            patch("app.github_auth.GITHUB_PRIVATE_KEY", _TEST_PRIVATE_PEM),
        ):
            from app.github_auth import generate_github_app_jwt

            return generate_github_app_jwt()

    def test_returns_string(self):
        """Output must be a plain str, not bytes — httpx Authorization headers
        require str and PyJWT 2.x returns str from encode()."""
        token = self._call()
        assert isinstance(token, str)

    def test_decodable_with_rs256(self):
        """Token must verify against the public key using RS256."""
        token = self._call()
        decoded = pyjwt.decode(
            token,
            _TEST_PUBLIC_PEM,
            algorithms=["RS256"],
            options={"verify_exp": True},
        )
        assert decoded is not None

    def test_iss_is_string(self):
        """iss must be a string, not an integer.

        GitHub's API requires ``iss`` to be the App ID as a string.  PyJWT 2.13
        tightened issuer validation — passing an int where a string is expected
        raises InvalidIssuerError.  The source sets ``iss = GITHUB_APP_ID``
        (already a str from os.environ), but if that ever regresses to an int
        this test catches it before the token reaches GitHub.
        """
        token = self._call()
        decoded = pyjwt.decode(
            token,
            _TEST_PUBLIC_PEM,
            algorithms=["RS256"],
            options={"verify_exp": True},
        )
        assert isinstance(decoded["iss"], str), (
            f"iss must be a str (got {type(decoded['iss']).__name__!r}); "
            "GitHub App auth will fail if it's an int"
        )

    def test_iss_matches_app_id(self):
        """iss must equal the GITHUB_APP_ID string."""
        token = self._call()
        decoded = pyjwt.decode(
            token,
            _TEST_PUBLIC_PEM,
            algorithms=["RS256"],
            options={"verify_exp": True},
        )
        assert decoded["iss"] == _TEST_APP_ID

    def test_iat_is_60_seconds_in_the_past(self):
        """iat must sit 60 ± 5 s before now to satisfy GitHub's clock-drift buffer.

        If a pyjwt or time.time() change shifts this offset the token will be
        rejected by GitHub ('iat is in the future' or 'iat is too old').
        """
        before = int(time.time())
        token = self._call()
        after = int(time.time())

        decoded = pyjwt.decode(
            token,
            _TEST_PUBLIC_PEM,
            algorithms=["RS256"],
            options={"verify_exp": True, "verify_iat": False},
        )
        iat = decoded["iat"]
        # iat should be ~60 s before call time
        assert before - 65 <= iat <= after - 55, (
            f"iat={iat} is not 60 ± 5 s before now ({before}); "
            "GitHub requires iat at least 60 s in the past to tolerate clock drift"
        )

    def test_exp_is_ten_minutes_ahead(self):
        """exp must be ≈10 min (600 s) after now (GitHub's max JWT TTL)."""
        before = int(time.time())
        token = self._call()
        after = int(time.time())

        decoded = pyjwt.decode(
            token,
            _TEST_PUBLIC_PEM,
            algorithms=["RS256"],
            options={"verify_exp": True},
        )
        exp = decoded["exp"]
        # exp = now + 600; give a ±10 s tolerance for test execution time
        assert before + 590 <= exp <= after + 610, (
            f"exp={exp} is not within 10 s of now+600 (got offset "
            f"{exp - before}); GitHub rejects JWTs with exp > 10 min"
        )

    def test_iat_and_exp_are_integers(self):
        """iat and exp must be integer Unix timestamps, not floats or strings."""
        token = self._call()
        decoded = pyjwt.decode(
            token,
            _TEST_PUBLIC_PEM,
            algorithms=["RS256"],
            options={"verify_exp": True, "verify_iat": False},
        )
        assert isinstance(decoded["iat"], int), (
            f"iat must be int, got {type(decoded['iat']).__name__}"
        )
        assert isinstance(decoded["exp"], int), (
            f"exp must be int, got {type(decoded['exp']).__name__}"
        )

    def test_missing_app_id_raises(self):
        """generate_github_app_jwt must raise ValueError when GITHUB_APP_ID is unset."""
        with (
            patch("app.github_auth.GITHUB_APP_ID", ""),
            patch("app.github_auth.GITHUB_PRIVATE_KEY", _TEST_PRIVATE_PEM),
        ):
            from app.github_auth import generate_github_app_jwt

            with pytest.raises(ValueError):
                generate_github_app_jwt()

    def test_missing_private_key_raises(self):
        """generate_github_app_jwt must raise ValueError when GITHUB_PRIVATE_KEY is unset."""
        with (
            patch("app.github_auth.GITHUB_APP_ID", _TEST_APP_ID),
            patch("app.github_auth.GITHUB_PRIVATE_KEY", ""),
        ):
            from app.github_auth import generate_github_app_jwt

            with pytest.raises(ValueError):
                generate_github_app_jwt()


# ---------------------------------------------------------------------------
# 2. Payload model_dump byte-size determinism
# ---------------------------------------------------------------------------


class TestPayloadSerializationSizeDeterminism:
    """Pydantic model_dump(exclude_none=True) must produce stable, bounded output.

    These tests guard against pydantic bumps that change field-exclusion
    semantics (e.g. extra-field tracking on 'extra=allow' models, or after-
    validator behavior changes in 2.12+).  If any of these assert the wrong size
    after a bump, investigate whether pydantic now serializes None-valued extras
    or after-validator side effects before assuming the code changed.
    """

    def test_create_payload_minimal_dump_excludes_none(self):
        """A CreatePayload with only 'prompt' set must serialize with exactly the
        keys that have values — no None-valued fields, no hidden extras."""
        from app.payload_schemas import CreatePayload

        payload = CreatePayload(prompt="fix the bug")
        dumped = payload.model_dump(exclude_none=True)

        assert "prompt" in dumped
        assert dumped["prompt"] == "fix the bug"
        # None-valued fields must be absent
        for key, val in dumped.items():
            assert val is not None, f"exclude_none=True leaked None for key {key!r}"

    def test_create_payload_with_all_base_fields_round_trips(self):
        """All BasePayload fields set should round-trip through model_dump."""
        from app.payload_schemas import CreatePayload

        payload = CreatePayload(
            prompt="implement the feature",
            base_branch="main",
            model="claude-sonnet-4-6",
            effort="high",
            title="my task",
        )
        dumped = payload.model_dump(exclude_none=True)
        assert dumped["prompt"] == "implement the feature"
        assert dumped["base_branch"] == "main"
        assert dumped["model"] == "claude-sonnet-4-6"
        assert dumped["effort"] == "high"
        assert dumped["title"] == "my task"

    def test_create_payload_serialized_size_is_bounded(self):
        """A compact CreatePayload must fit well under the 190 KB budget."""
        from app.payload_schemas import CreatePayload, _MAX_PAYLOAD_BYTES

        payload = CreatePayload(prompt="x" * 1000, base_branch="main")
        dumped = payload.model_dump(exclude_none=True)
        dumped["type"] = "create"
        size = len(json.dumps(dumped, default=str).encode())
        assert size < _MAX_PAYLOAD_BYTES, (
            f"A 1 KB prompt payload serialized to {size} bytes, "
            f"which exceeds the {_MAX_PAYLOAD_BYTES}-byte budget"
        )
        # Must stay well under the budget — flag if it blows past 10 KB
        assert size < 10 * 1024, (
            f"A 1 KB prompt payload serialized to {size} bytes — "
            "this is suspiciously large; did pydantic start including extras?"
        )

    def test_max_payload_bytes_constant_is_190_kb(self):
        """_MAX_PAYLOAD_BYTES must equal 190 * 1024 = 194560.

        This constant must stay aligned with the DB constraint
        (chk_task_payloads_size: octet_length < 200000).  A pydantic bump that
        causes all payloads to grow without touching this constant would pass
        validate_task_payload but fail at DB insert with an opaque IntegrityError.
        """
        from app.payload_schemas import _MAX_PAYLOAD_BYTES

        assert _MAX_PAYLOAD_BYTES == 190 * 1024, (
            f"_MAX_PAYLOAD_BYTES is {_MAX_PAYLOAD_BYTES}, expected {190 * 1024}; "
            "if you raise this, raise the DB constraint in a new migration first"
        )

    def test_oversized_payload_raises_payload_too_large_error(self):
        """A payload exceeding _MAX_PAYLOAD_BYTES must raise PayloadTooLargeError."""
        from app.payload_schemas import PayloadTooLargeError, validate_task_payload

        big_prompt = "x" * (200 * 1024)
        with pytest.raises(PayloadTooLargeError):
            validate_task_payload({"type": "create", "prompt": big_prompt})

    def test_strategic_review_payload_dump_excludes_none_fields(self):
        """StrategicReviewPayload with sparse fields must not bloat with None extras."""
        from app.payload_schemas import StrategicReviewPayload

        payload = StrategicReviewPayload(
            project_name="test-project",
            is_self_repo=True,
        )
        dumped = payload.model_dump(exclude_none=True)

        assert dumped["project_name"] == "test-project"
        assert dumped["is_self_repo"] is True
        # None-valued optional fields must be absent
        for key, val in dumped.items():
            assert val is not None, f"exclude_none=True leaked None for key {key!r}"

    def test_model_dump_size_is_deterministic(self):
        """The same payload must serialize to the same byte count on repeated calls.

        Non-determinism here would indicate pydantic is including runtime state
        (e.g. extra-field tracking sets, memo caches) in the serialized output.
        """
        from app.payload_schemas import CreatePayload

        payload = CreatePayload(prompt="hello world", effort="low")
        size_a = len(json.dumps(payload.model_dump(exclude_none=True)).encode())
        size_b = len(json.dumps(payload.model_dump(exclude_none=True)).encode())
        assert size_a == size_b, (
            f"model_dump produced different sizes across calls: {size_a} vs {size_b}"
        )

    def test_acceptance_evidence_roundtrips_through_model_dump(self):
        """AcceptanceEvidence nested in CreatePayload must survive model_dump."""
        from app.payload_schemas import AcceptanceEvidence, CreatePayload

        evidence = AcceptanceEvidence(
            kind="named_test",
            test_path="backend/testing/test_something.py",
            test_id="test_my_feature",
        )
        payload = CreatePayload(prompt="add the feature", acceptance_evidence=evidence)
        dumped = payload.model_dump(exclude_none=True)

        assert "acceptance_evidence" in dumped
        ae = dumped["acceptance_evidence"]
        assert ae["kind"] == "named_test"
        assert ae["test_path"] == "backend/testing/test_something.py"
        assert ae["test_id"] == "test_my_feature"

    def test_validate_task_payload_preserves_type_field(self):
        """validate_task_payload must re-inject 'type' into the dumped output.

        Pydantic models don't carry 'type' as a field — validate_task_payload
        manually re-injects it after model_dump.  A pydantic bump that starts
        silently including 'type' as an extra-field (extra='allow') would
        duplicate the field and potentially double-count its bytes.
        """
        from app.payload_schemas import validate_task_payload

        result = validate_task_payload(
            {
                "type": "create",
                "prompt": "do the thing",
                # A code-producing payload must declare acceptance_evidence on
                # the strict validation surface.
                "acceptance_evidence": {
                    "kind": "observable_behavior",
                    "description": "ships a real, testable change",
                },
            }
        )
        assert result["type"] == "create"
        assert result["prompt"] == "do the thing"
        # Exactly one 'type' key
        assert list(result).count("type") == 1 if "type" in result else True


class TestCodeProducingDependencyMergeWait:
    """``_check_candidate_task_deps`` (orchestrator_health.py) must hold a
    same-base dependent behind ANY code-producing prerequisite type — not just
    ``create`` — until that prerequisite's PR is merged, since the dependent's
    base only ever sees the prerequisite's code once its PR lands. Widening
    the gate must not deadlock a dependent behind a prerequisite type that
    never ships a PR (pure-sequencing types keep releasing on COMPLETED)."""

    async def _verify_true(self, _task, _session):
        return True

    async def _verify_false(self, _task, _session):
        return False

    @pytest.mark.asyncio
    async def test_refactor_prerequisite_holds_dependent_until_pr_merged(
        self, db_session
    ):
        from app.orchestrator import _check_candidate_task_deps
        from testing.factories import make_project, make_task
        from app.models import TaskStatus

        project = make_project(name="RefactorMergeWait", user_id="u1")
        db_session.add(project)
        await db_session.commit()

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "refactor", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
        )
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([dep, dependent])
        await db_session.commit()

        # A COMPLETED but unmerged refactor prerequisite must still hold the
        # dependent — this is the defect: before the fix the merge-wait was
        # gated on dt_type in ("create", ""), so a refactor dep released the
        # dependent as soon as it completed, regardless of PR state.
        waiting, _ = await _check_candidate_task_deps(
            dependent, [dep], [dep.id], db_session, self._verify_false
        )
        assert waiting == "waiting"

        # Once its PR is merged, the dependent becomes eligible.
        eligible, _ = await _check_candidate_task_deps(
            dependent, [dep], [dep.id], db_session, self._verify_true
        )
        assert eligible == "eligible"

    @pytest.mark.asyncio
    async def test_research_prerequisite_releases_dependent_on_completion(
        self, db_session
    ):
        """Pure-sequencing prerequisite types (research, plan, review, ...)
        never ship a PR, so they must keep releasing their dependent as soon
        as they COMPLETE — widening the merge-wait to more code-producing
        types must not regress this."""
        from app.orchestrator import _check_candidate_task_deps
        from testing.factories import make_project, make_task
        from app.models import TaskStatus

        project = make_project(name="ResearchNoMergeWait", user_id="u1")
        db_session.add(project)
        await db_session.commit()

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
        )
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([dep, dependent])
        await db_session.commit()

        # verify_false would fail the test if the research dep were (wrongly)
        # routed through the PR-merge wait; it must never even be consulted.
        eligible, _ = await _check_candidate_task_deps(
            dependent, [dep], [dep.id], db_session, self._verify_false
        )
        assert eligible == "eligible"

    @pytest.mark.asyncio
    async def test_qa_prerequisite_with_no_pr_artifact_does_not_deadlock(
        self, db_session
    ):
        """A code-producing (qa) prerequisite that completed WITHOUT ever
        opening a PR (e.g. no diff was needed) must not wedge its dependent
        forever — ``verify_pr_merged`` treats a missing PR artifact as done,
        and that must still apply once the gate covers qa, not just create."""
        from app.orchestrator import _check_candidate_task_deps
        from testing.factories import make_project, make_task
        from app.models import TaskStatus

        project = make_project(name="QANoPRArtifact", user_id="u1")
        db_session.add(project)
        await db_session.commit()

        dep = make_task(
            project_id=project.id,
            payloads=[{"type": "qa", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.COMPLETED,
        )
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "research/aaa"}],
            context={"related_files": ["backend/app/foo.py"]},
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([dep, dependent])
        await db_session.commit()

        # No PULL_REQUEST artifact was created for dep — the real
        # verify_pr_merged (not a stub) must treat that as "done" rather than
        # holding the dependent indefinitely.
        from app.rollup_pr_verification import _verify_pr_merged

        eligible, _ = await _check_candidate_task_deps(
            dependent, [dep], [dep.id], db_session, _verify_pr_merged
        )
        assert eligible == "eligible"


# ---------------------------------------------------------------------------
# Helper: fake async session factory (mirrors test_orchestrator._FakeSessionFactory)
# ---------------------------------------------------------------------------


class _FakeSessionFactory:
    """Replaces AsyncSessionLocal so background services use the test session."""

    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False


# ---------------------------------------------------------------------------
# 4. Warm-VM ordering: ensure_dependent_base_recut BEFORE _deps_satisfied
# ---------------------------------------------------------------------------


class TestDependentWorkspaceContainsPrerequisiteMergedCode:
    """Pin the ORDERING invariant that guards the campaign sequencing contract:

      ``ensure_dependent_base_recut`` MUST return True before the
      ``_deps_satisfied`` warm-claim marker is ever stamped on a cross-base
      dependent.

    Chain under test (links verified by reading at HEAD, not assumed):

      1. ``_inject_build_on_deps`` auto-authors the dependency edge when a
         later subtask's prompt references a file a prior subtask produces
         (plan_completion.py:158-205).
      2. ``_maybe_inherit_base`` gives a cross-base code-dependent the
         foundation's base_branch so same-base merge-wait logic applies
         (plan_completion.py:1254-1279).
      3. ``_check_candidate_task_deps`` holds the dependent until the
         prerequisite's PR is merged (same-base) or its code has landed on
         integration (cross-base, via ``has_landed_signal``)
         (orchestrator_health.py:1122-1180).
      4. ``ensure_dependent_base_recut`` merges current integration into the
         dependent's base BEFORE ``_deps_satisfied`` is stamped; if it fails,
         the marker is withheld and a warm VM cannot claim the task
         (dep_lifecycle.py:509-531, orchestrator.py:2442-2480).
      5. Agent resolves base_branch as a NAME and fetches origin/<branch>
         fresh at run time (agent/src/workspacePrep.ts:302-325 — out of scope
         for this Python suite; covered by workspacePrep.test.ts).

    Fragility notes (not fixed here — recorded for downstream reviewers):
      * The ``_deps_satisfied`` marker is the sole mechanism a warm VM uses to
        skip full dep-resolution on the /next-role hot path. Any path that
        stamps the marker without a preceding recut check lets a warm VM
        dispatch against a stale base. There are TWO independent stamp sites:
        ``dep_lifecycle._mark_unblocked_dependents`` (completion-side trigger)
        and the main dispatch loop in ``orchestrator.py``. Drift between them
        is not detected by existing tests — a refactor that adds a third site
        must also add a recut guard.
      * ``_check_candidate_task_deps`` (orchestrator_health.py:1156-1173) uses
        ``_is_code_producing_task`` to decide whether a same-base COMPLETED dep
        needs its PR merged.  Cross-base deps are routed differently (via
        ``has_landed_signal``).  Adding a new code-producing task type to
        either branch but not the other would silently create a blind spot.
      * The dt_type gate for same-base merge-wait was widened from ``('create',
        '')`` to all code-producing types in PR #16936. The cross-base
        ``has_landed_signal`` gate is not type-gated and is therefore
        unaffected. Out of scope for the dt_type extension: ordering-only
        cross-base deps (no file overlap) continue to release immediately.
    """

    async def _verify_true(self, _task, _session):
        return True

    # ------------------------------------------------------------------
    # Link 1: _inject_build_on_deps auto-authors the dep edge at plan time
    # ------------------------------------------------------------------

    def test_inject_build_on_deps_adds_edge_when_prompt_references_producer_file(self):
        """A consumer subtask whose prompt mentions a producer's related_file gets
        a depends_on_indices edge pointing at the producer, enabling the
        dispatch merge-wait to hold it until the producer's code is merged.

        This exercises link 1 of the chain: plan_completion._inject_build_on_deps.
        """
        from app.plan_completion import _inject_build_on_deps

        subtasks = [
            {
                "title": "create new_module",
                "related_files": ["backend/app/new_module.py"],
                "prompt": "Create the new_module.py module.",
            },
            {
                "title": "integrate new_module",
                "related_files": ["backend/app/integration.py"],
                "prompt": "Update integration.py to import backend/app/new_module.py.",
            },
        ]
        added = _inject_build_on_deps(subtasks)

        assert len(added) == 1, (
            f"Expected one build-on edge from producer→consumer, got {added!r}"
        )
        producer_idx, consumer_idx, path = added[0]
        assert producer_idx == 0
        assert consumer_idx == 1
        assert path == "backend/app/new_module.py"
        assert subtasks[1].get("depends_on_indices") == [0], (
            "consumer must carry depends_on_indices=[0] after injection; "
            "without it the dispatch engine sees no ordering constraint and "
            "may dispatch both subtasks in parallel"
        )

    def test_inject_build_on_deps_skips_shared_file(self):
        """When consumer already lists the file in related_files, the same-file
        overlap pass handles it; _inject_build_on_deps must NOT add a duplicate edge.
        """
        from app.plan_completion import _inject_build_on_deps

        subtasks = [
            {
                "title": "modify shared.py",
                "related_files": ["backend/app/shared.py"],
                "prompt": "Refactor shared.py.",
            },
            {
                "title": "also modify shared.py",
                "related_files": ["backend/app/shared.py"],
                "prompt": "Also touch backend/app/shared.py for a follow-up.",
            },
        ]
        added = _inject_build_on_deps(subtasks)

        assert added == [], (
            "_inject_build_on_deps must not duplicate same-file edges; "
            "_inject_file_overlap_deps already owns that case"
        )

    def test_inject_build_on_deps_no_edge_when_prompt_does_not_reference_file(self):
        """Independent subtasks (prompt does not mention the producer's file)
        must NOT receive an artificial ordering constraint.
        """
        from app.plan_completion import _inject_build_on_deps

        subtasks = [
            {
                "title": "create alpha.py",
                "related_files": ["backend/app/alpha.py"],
                "prompt": "Create the alpha module.",
            },
            {
                "title": "create beta.py",
                "related_files": ["backend/app/beta.py"],
                "prompt": "Create the beta module independently.",
            },
        ]
        added = _inject_build_on_deps(subtasks)

        assert added == [], (
            "Independent subtasks must stay parallel; adding a spurious dep "
            "would serialize unrelated work and reduce plan throughput"
        )

    # ------------------------------------------------------------------
    # Link 2: _maybe_inherit_base (cross-base base_branch propagation)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_check_candidate_task_deps_cross_base_held_until_landed(
        self, db_session
    ):
        """Link 2+3 combined: a cross-base genuine code dep holds the dependent
        until ``has_landed_signal`` is True on the prerequisite.

        Verifies that a COMPLETED cross-base code dep WITHOUT the landed signal
        keeps the dependent waiting, and becomes eligible once the signal is set.
        """
        from app.orchestrator import _check_candidate_task_deps
        from app.landed_signal import mark_landed_on_integration
        from testing.factories import make_project, make_task
        from app.models import TaskStatus

        project = make_project(name="CrossBaseHoldTest", user_id="u1")
        db_session.add(project)
        await db_session.commit()

        prereq = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "plan/feat-a"}],
            context={"related_files": ["backend/app/new_module.py"]},
            status=TaskStatus.COMPLETED,
        )
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "fix", "base_branch": "plan/feat-b"}],
            context={"related_files": ["backend/app/new_module.py"]},
            status=TaskStatus.QUEUED,
        )
        db_session.add_all([prereq, dependent])
        await db_session.commit()

        async def _verify_false(t, s):
            return False

        # COMPLETED but not landed → still waiting.
        waiting, _ = await _check_candidate_task_deps(
            dependent, [prereq], [prereq.id], db_session, _verify_false
        )
        assert waiting == "waiting", (
            "cross-base code dep COMPLETED but not landed must keep the "
            "dependent waiting; dispatching now would put B against a base "
            "that does NOT yet contain A's merged code"
        )

        # Once the dep has landed, the dependent becomes eligible.
        mark_landed_on_integration(prereq)
        db_session.add(prereq)
        await db_session.commit()

        eligible, _ = await _check_candidate_task_deps(
            dependent, [prereq], [prereq.id], db_session, _verify_false
        )
        assert eligible == "eligible", (
            "cross-base code dep with has_landed_signal must release the "
            "dependent so it can be dispatched against an updated base"
        )

    # ------------------------------------------------------------------
    # Link 4 (THE NAMED TEST): ordering invariant — recut before marker
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_dependent_workspace_contains_prerequisite_merged_code(
        self, db_session, mocker
    ):
        """``_deps_satisfied`` is stamped ONLY AFTER ``ensure_dependent_base_recut``
        returns True for a cross-base genuine code dependency.

        Scenario: prerequisite A is COMPLETED and has landed on integration;
        dependent B has a cross-base genuine code dependency on A (different
        base_branch, overlapping related_files). When the completion-side
        trigger ``_mark_unblocked_dependents`` fires:

          (a) If the re-cut FAILS (returns False), ``_deps_satisfied`` is NOT
              stamped. A warm VM polling via the /next-role backlog scan cannot
              see B as warm-claimable, so it cannot dispatch B against a base
              that is still MISSING A's landed code.

          (b) If the re-cut SUCCEEDS (returns True), ``_deps_satisfied`` IS
              stamped. B is now safely warm-claimable: its base has been
              updated from integration and provably contains A's code.

        This directly exercises the ordering invariant described in
        orchestrator.py:2440-2480 and dep_lifecycle.py:509-531.

        A regression that stamps the marker before the re-cut (or skips the
        re-cut entirely) would allow a warm VM to claim B and run it against
        a stale base — producing a silently-broken build that bypasses both
        the file-overlap conflict guard and the deploy-time integration test.
        """
        from app.dep_lifecycle import _mark_unblocked_dependents
        from app.landed_signal import mark_landed_on_integration
        from testing.factories import make_project, make_task
        from app.models import TaskStatus

        project = make_project(
            name="WarmVMOrderingTest", user_id="u1", integration_branch="main"
        )
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        # Prerequisite A: COMPLETED and landed on integration.
        # base_branch "plan/feat-a" differs from the dependent's "plan/feat-b".
        prereq = make_task(
            project_id=project.id,
            payloads=[{"type": "create", "base_branch": "plan/feat-a"}],
            context={"related_files": ["backend/app/new_module.py"]},
            status=TaskStatus.COMPLETED,
        )
        mark_landed_on_integration(prereq)
        db_session.add(prereq)
        await db_session.commit()
        await db_session.refresh(prereq)

        # Trigger task: same base as dependent, just completed — fires the sweep.
        trigger = make_task(
            project_id=project.id,
            payloads=[{"type": "research", "base_branch": "plan/feat-b"}],
            status=TaskStatus.COMPLETED,
        )
        db_session.add(trigger)
        await db_session.commit()
        await db_session.refresh(trigger)

        # Dependent B: cross-base genuine code dep on A.
        # related_files overlaps prereq → is_genuine_code_dependency = True.
        dependent = make_task(
            project_id=project.id,
            payloads=[{"type": "fix", "base_branch": "plan/feat-b"}],
            context={"related_files": ["backend/app/new_module.py"]},
            status=TaskStatus.QUEUED,
            depends_on=[str(prereq.id), str(trigger.id)],
        )
        db_session.add(dependent)
        await db_session.commit()
        await db_session.refresh(dependent)

        mocker.patch(
            "app.dep_lifecycle.AsyncSessionLocal",
            _FakeSessionFactory(db_session),
        )

        # (a) Re-cut FAILS → _deps_satisfied must NOT be set.
        # Patching app.orchestrator_health.ensure_dependent_base_recut
        # intercepts the lazy import inside _mark_unblocked_dependents.
        mocker.patch(
            "app.orchestrator_health.ensure_dependent_base_recut",
            new_callable=AsyncMock,
            return_value=False,
        )
        marked = await _mark_unblocked_dependents(trigger, self._verify_true)
        assert marked == 0, (
            "warm-claim count must be 0 when ensure_dependent_base_recut "
            f"returns False (got {marked}); the dependent must remain "
            "unheld so a warm VM cannot dispatch it against a stale base"
        )
        db_session.expire(dependent)
        await db_session.refresh(dependent)
        assert not (dependent.context or {}).get("_deps_satisfied"), (
            "_deps_satisfied must be ABSENT when the re-cut is incomplete; "
            "stamping it before the re-cut succeeds violates the ordering "
            "invariant (orchestrator.py:2447, dep_lifecycle.py:528) and "
            "would let a warm VM run dependent B against a base that is "
            "missing prerequisite A's landed code"
        )

        # (b) Re-cut SUCCEEDS → _deps_satisfied MUST be set.
        mocker.patch(
            "app.orchestrator_health.ensure_dependent_base_recut",
            new_callable=AsyncMock,
            return_value=True,
        )
        marked = await _mark_unblocked_dependents(trigger, self._verify_true)
        assert marked == 1, (
            "warm-claim count must be 1 when ensure_dependent_base_recut "
            f"returns True (got {marked}); the dependent is now safe to "
            "dispatch against its re-cut base"
        )
        db_session.expire(dependent)
        await db_session.refresh(dependent)
        assert (dependent.context or {}).get("_deps_satisfied") is True, (
            "_deps_satisfied must be present after ensure_dependent_base_recut "
            "succeeds; its absence would force cold provisioning for every "
            "follow-on task even when a warm VM is available"
        )
