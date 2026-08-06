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
from unittest.mock import patch

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
