"""Tests for the field-level encryption module ``app.field_encryption``.

Covers encrypt/decrypt round-trip with a valid Fernet key, None passthrough,
missing-key behaviour with ``ALLOW_PLAINTEXT_CREDENTIALS`` opt-in, InvalidToken
plaintext fallback, and ``is_encryption_active``.
"""

import pytest
from cryptography.fernet import Fernet
from app.field_encryption import (
    encrypt_field,
    decrypt_field,
    is_encryption_active,
    require_encryption_active,
    assert_plaintext_or_raise,
    probe_key_continuity,
    ENCRYPTION_KEY_ENV,
    ALLOW_PLAINTEXT_CREDENTIALS_ENV,
    CONTINUITY_REFERENCE_PLAINTEXT,
    FERNET_CIPHERTEXT_PREFIX,
)


class TestEncryptDecryptRoundTrip:
    """encrypt_field / decrypt_field with a valid encryption key."""

    def test_round_trip_basic(self, monkeypatch):
        """Encrypt then decrypt a typical token value returns the original."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

        original = "ghp_test_token_12345"
        encrypted = encrypt_field(original)
        assert encrypted is not None
        assert encrypted != original  # ciphertext differs from plaintext
        decrypted = decrypt_field(encrypted)
        assert decrypted == original

    def test_round_trip_empty_string(self, monkeypatch):
        """Empty string round-trips correctly through encrypt then decrypt."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

        encrypted = encrypt_field("")
        assert encrypted is not None
        assert encrypted != ""
        decrypted = decrypt_field(encrypted)
        assert decrypted == ""

    def test_round_trip_long_value(self, monkeypatch):
        """Long token round-trips correctly (regression: no truncation)."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

        original = "x" * 2000
        encrypted = encrypt_field(original)
        decrypted = decrypt_field(encrypted)
        assert decrypted == original

    def test_round_trip_special_characters(self, monkeypatch):
        """Tokens with special characters round-trip correctly."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

        original = "ghp_!@#$%^&*()_+-=[]{}|;':\",./<>?`~你好"
        encrypted = encrypt_field(original)
        decrypted = decrypt_field(encrypted)
        assert decrypted == original


class TestNonePassthrough:
    """Both functions return None when given None (guard runs before key logic)."""

    def test_encrypt_none(self):
        """encrypt_field(None) returns None regardless of key configuration."""
        assert encrypt_field(None) is None

    def test_decrypt_none(self):
        """decrypt_field(None) returns None regardless of key configuration."""
        assert decrypt_field(None) is None


class TestMissingKeyNoop:
    """Both functions are no-ops when GITHUB_PAT_ENCRYPTION_KEY is not set
    but ALLOW_PLAINTEXT_CREDENTIALS is explicitly enabled."""

    def test_encrypt_noop(self, monkeypatch):
        """encrypt_field returns the value unchanged when key is absent
        and ALLOW_PLAINTEXT_CREDENTIALS is set."""
        monkeypatch.delenv(ENCRYPTION_KEY_ENV, raising=False)
        monkeypatch.setenv(ALLOW_PLAINTEXT_CREDENTIALS_ENV, "true")
        original = "plaintext_token"
        # Identity check: the function returns the exact same object, not a copy
        assert encrypt_field(original) is original

    def test_decrypt_noop(self, monkeypatch):
        """decrypt_field returns the value unchanged when key is absent
        and ALLOW_PLAINTEXT_CREDENTIALS is set."""
        monkeypatch.delenv(ENCRYPTION_KEY_ENV, raising=False)
        monkeypatch.setenv(ALLOW_PLAINTEXT_CREDENTIALS_ENV, "true")
        original = "plaintext_token"
        assert decrypt_field(original) is original

    def test_encrypt_empty_string_noop(self, monkeypatch):
        """encrypt_field('') returns '' (the same object) when key is absent
        and ALLOW_PLAINTEXT_CREDENTIALS is set."""
        monkeypatch.delenv(ENCRYPTION_KEY_ENV, raising=False)
        monkeypatch.setenv(ALLOW_PLAINTEXT_CREDENTIALS_ENV, "true")
        assert encrypt_field("") == ""

    def test_decrypt_empty_string_noop(self, monkeypatch):
        """decrypt_field('') returns '' (the same object) when key is absent
        and ALLOW_PLAINTEXT_CREDENTIALS is set."""
        monkeypatch.delenv(ENCRYPTION_KEY_ENV, raising=False)
        monkeypatch.setenv(ALLOW_PLAINTEXT_CREDENTIALS_ENV, "true")
        assert decrypt_field("") == ""

    def test_encrypt_raises_without_key_and_without_opt_in(self, monkeypatch):
        """encrypt_field raises RuntimeError when key is absent
        and ALLOW_PLAINTEXT_CREDENTIALS is not set."""
        monkeypatch.delenv(ENCRYPTION_KEY_ENV, raising=False)
        monkeypatch.delenv(ALLOW_PLAINTEXT_CREDENTIALS_ENV, raising=False)
        with pytest.raises(RuntimeError, match="GITHUB_PAT_ENCRYPTION_KEY"):
            encrypt_field("sensitive")

    def test_decrypt_raises_without_key_and_without_opt_in(self, monkeypatch):
        """decrypt_field raises RuntimeError when key is absent
        and ALLOW_PLAINTEXT_CREDENTIALS is not set."""
        monkeypatch.delenv(ENCRYPTION_KEY_ENV, raising=False)
        monkeypatch.delenv(ALLOW_PLAINTEXT_CREDENTIALS_ENV, raising=False)
        with pytest.raises(RuntimeError, match="GITHUB_PAT_ENCRYPTION_KEY"):
            decrypt_field("sensitive")


class TestInvalidTokenFallback:
    """decrypt_field handles non-Fernet values gracefully."""

    def test_decrypt_plaintext_returns_unchanged(self, monkeypatch):
        """decrypt_field on a plaintext value returns it as-is (backward compat)."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

        plaintext = "not_encrypted_token"
        result = decrypt_field(plaintext)
        assert result == plaintext

    def test_decrypt_malformed_returns_unchanged(self, monkeypatch):
        """decrypt_field on a malformed Fernet token returns it as-is."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

        malformed = "this-is-not-a-valid-fernet-token-at-all"
        result = decrypt_field(malformed)
        assert result == malformed

    def test_decrypt_plaintext_empty_string(self, monkeypatch):
        """decrypt_field('') on plaintext empty string returns ''."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

        result = decrypt_field("")
        assert result == ""


class TestMaxPlanAccountFieldEncryption:
    """Verify encrypt/decrypt behaviour for MaxPlanAccount oauth_token and
    claude_config_json fields, which must be encrypted at rest when a key is set."""

    def test_oauth_token_is_encrypted_at_rest(self, monkeypatch):
        """oauth_token written to DB is ciphertext, not the Secret Manager path."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

        secret_manager_ref = "projects/p/secrets/llm-account-uuid/versions/1"
        stored = encrypt_field(secret_manager_ref)
        assert stored is not None
        assert stored != secret_manager_ref
        assert not stored.startswith("projects/")

    def test_oauth_token_decrypts_to_original(self, monkeypatch):
        """Decrypting a stored oauth_token recovers the Secret Manager path."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

        ref = "projects/p/secrets/llm-account-uuid/versions/1"
        recovered = decrypt_field(encrypt_field(ref))
        assert recovered == ref

    def test_claude_config_json_is_encrypted_at_rest(self, monkeypatch):
        """claude_config_json written to DB is ciphertext, not raw JSON."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

        config = '{"onboardingAccepted":true,"email":"user@example.com"}'
        stored = encrypt_field(config)
        assert stored is not None
        assert stored != config
        assert "onboardingAccepted" not in stored

    def test_claude_config_json_decrypts_to_original(self, monkeypatch):
        """Decrypting a stored claude_config_json recovers the original JSON string."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

        config = '{"onboardingAccepted":true}'
        recovered = decrypt_field(encrypt_field(config))
        assert recovered == config

    def test_legacy_plaintext_oauth_token_decrypts_unchanged(self, monkeypatch):
        """Pre-encryption rows (plaintext oauth_token) are returned as-is on read."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

        legacy_ref = "projects/p/secrets/old-account/versions/1"
        result = decrypt_field(legacy_ref)
        assert result == legacy_ref


class TestUnexpectedDecryptionErrorPropagates:
    """Errors outside (InvalidToken, ValueError, TypeError) must propagate."""

    def test_unexpected_exception_propagates(self, monkeypatch):
        """An AttributeError from f.decrypt() propagates instead of being swallowed."""
        from unittest.mock import MagicMock, patch
        from cryptography.fernet import Fernet as _Fernet

        key = _Fernet.generate_key().decode()
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

        mock_fernet = MagicMock()
        mock_fernet.decrypt.side_effect = AttributeError("library API changed")

        with patch("app.field_encryption._get_fernet", return_value=mock_fernet):
            with pytest.raises(AttributeError, match="library API changed"):
                decrypt_field("some_value")

    def test_value_error_returns_unchanged(self, monkeypatch):
        """A ValueError from encoding is caught and returns the value as-is."""
        from unittest.mock import MagicMock, patch
        from cryptography.fernet import Fernet as _Fernet

        key = _Fernet.generate_key().decode()
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

        mock_fernet = MagicMock()
        mock_fernet.decrypt.side_effect = ValueError("bad encoding")

        with patch("app.field_encryption._get_fernet", return_value=mock_fernet):
            result = decrypt_field("some_value")
            assert result == "some_value"

    def test_type_error_returns_unchanged(self, monkeypatch):
        """A TypeError is caught and returns the value as-is."""
        from unittest.mock import MagicMock, patch
        from cryptography.fernet import Fernet as _Fernet

        key = _Fernet.generate_key().decode()
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

        mock_fernet = MagicMock()
        mock_fernet.decrypt.side_effect = TypeError("unexpected type")

        with patch("app.field_encryption._get_fernet", return_value=mock_fernet):
            result = decrypt_field("some_value")
            assert result == "some_value"


class TestIsEncryptionActive:
    """is_encryption_active reports whether encryption is properly configured."""

    def test_active_with_valid_key(self, monkeypatch):
        """is_encryption_active returns True when a valid key is set."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv(ENCRYPTION_KEY_ENV, key)
        assert is_encryption_active() is True

    def test_inactive_without_key(self, monkeypatch):
        """is_encryption_active returns False when no key is set."""
        monkeypatch.delenv(ENCRYPTION_KEY_ENV, raising=False)
        assert is_encryption_active() is False

    def test_inactive_with_invalid_key(self, monkeypatch):
        """is_encryption_active returns False when the key is not valid Fernet."""
        monkeypatch.setenv(ENCRYPTION_KEY_ENV, "not-a-valid-fernet-key")
        assert is_encryption_active() is False


class TestWrongKeyPassthrough:
    """Documents the two-tier safety model for wrong-key decryption.

    decrypt_field itself passes ciphertext through unchanged when InvalidToken
    fires (backward compat for pre-migration plaintext rows).  The boot-time
    continuity probe is the primary safety net that catches a wrong-key rotation
    before any per-row decryption is attempted.
    """

    def test_wrong_key_decrypt_passes_ciphertext_through(self, monkeypatch):
        """decrypt_field with a wrong-but-valid Fernet key returns ciphertext unchanged.

        InvalidToken passthrough is intentional backward compat for pre-migration
        plaintext rows.  The boot-time continuity probe (probe_key_continuity) is
        the actual safety net that catches wrong-key rotation before any per-row
        decryption is attempted.
        """
        correct_key = Fernet.generate_key()
        wrong_key = Fernet.generate_key()

        f_correct = Fernet(correct_key)
        plaintext = "sensitive_token_value"
        ciphertext = f_correct.encrypt(plaintext.encode()).decode()

        # Configure the wrong key so decrypt_field will hit InvalidToken
        monkeypatch.setenv(ENCRYPTION_KEY_ENV, wrong_key.decode())

        result = decrypt_field(ciphertext)
        assert result == ciphertext, (
            "decrypt_field must return ciphertext unchanged on InvalidToken; "
            "the continuity probe is the safety net, not per-call raising"
        )

    def test_wrong_key_decrypt_emits_warning(self, monkeypatch, caplog):
        """decrypt_field logs a WARNING (not debug) when InvalidToken fires."""
        import logging

        correct_key = Fernet.generate_key()
        wrong_key = Fernet.generate_key()

        f_correct = Fernet(correct_key)
        ciphertext = f_correct.encrypt(b"secret").decode()

        monkeypatch.setenv(ENCRYPTION_KEY_ENV, wrong_key.decode())

        with caplog.at_level(logging.WARNING, logger="app.field_encryption"):
            decrypt_field(ciphertext)

        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_records, "Expected at least one WARNING log from decrypt_field on InvalidToken"
        assert ENCRYPTION_KEY_ENV in warning_records[0].getMessage()


class TestRequireEncryptionActive:
    """require_encryption_active fails closed on malformed/absent keys."""

    def test_backfill_fails_closed_when_encryption_expected_but_inactive(
        self, monkeypatch
    ):
        """RuntimeError when key is set-but-malformed, even with ALLOW_PLAINTEXT_CREDENTIALS.

        This is the canonical acceptance case: a mis-keyed environment must never
        silently stamp a backfill migration as complete while credentials remain plaintext.
        """
        monkeypatch.setenv(ENCRYPTION_KEY_ENV, "not-a-valid-fernet-key")
        monkeypatch.setenv(ALLOW_PLAINTEXT_CREDENTIALS_ENV, "true")
        with pytest.raises(RuntimeError, match=ENCRYPTION_KEY_ENV):
            require_encryption_active("test context")

    def test_raises_on_malformed_key_without_plaintext_opt_in(self, monkeypatch):
        """RuntimeError when key is malformed and ALLOW_PLAINTEXT_CREDENTIALS is absent."""
        monkeypatch.setenv(ENCRYPTION_KEY_ENV, "bad-key")
        monkeypatch.delenv(ALLOW_PLAINTEXT_CREDENTIALS_ENV, raising=False)
        with pytest.raises(RuntimeError, match=ENCRYPTION_KEY_ENV):
            require_encryption_active("migration 0531")

    def test_raises_on_absent_key_without_plaintext_opt_in(self, monkeypatch):
        """RuntimeError when key is absent and ALLOW_PLAINTEXT_CREDENTIALS is not set."""
        monkeypatch.delenv(ENCRYPTION_KEY_ENV, raising=False)
        monkeypatch.delenv(ALLOW_PLAINTEXT_CREDENTIALS_ENV, raising=False)
        with pytest.raises(RuntimeError, match=ENCRYPTION_KEY_ENV):
            require_encryption_active("migration 0621")

    def test_passes_when_key_is_absent_and_plaintext_opted_in(self, monkeypatch):
        """No error in dev mode: key absent, ALLOW_PLAINTEXT_CREDENTIALS set."""
        monkeypatch.delenv(ENCRYPTION_KEY_ENV, raising=False)
        monkeypatch.setenv(ALLOW_PLAINTEXT_CREDENTIALS_ENV, "true")
        require_encryption_active("dev context")  # must not raise

    def test_passes_when_key_is_valid(self, monkeypatch):
        """No error when a valid Fernet key is configured."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv(ENCRYPTION_KEY_ENV, key)
        monkeypatch.delenv(ALLOW_PLAINTEXT_CREDENTIALS_ENV, raising=False)
        require_encryption_active("migration check")  # must not raise

    def test_error_message_names_encryption_key_env(self, monkeypatch):
        """Error message includes GITHUB_PAT_ENCRYPTION_KEY for operator guidance."""
        monkeypatch.setenv(ENCRYPTION_KEY_ENV, "malformed")
        with pytest.raises(RuntimeError) as exc_info:
            require_encryption_active("Migration 0531")
        assert ENCRYPTION_KEY_ENV in str(exc_info.value)

    def test_context_string_is_included_in_error(self, monkeypatch):
        """The context argument appears in the RuntimeError message."""
        monkeypatch.setenv(ENCRYPTION_KEY_ENV, "malformed")
        with pytest.raises(RuntimeError, match="Migration 0531"):
            require_encryption_active("Migration 0531")


class TestAssertPlaintextOrRaise:
    """Wrong-key ciphertext must not be treated as plaintext by backfills."""

    def test_wrong_valid_key_raises(self, monkeypatch):
        key_a = Fernet.generate_key().decode()
        key_b = Fernet.generate_key().decode()
        monkeypatch.setenv(ENCRYPTION_KEY_ENV, key_a)
        ciphertext = encrypt_field("sensitive_secret")
        assert ciphertext is not None
        assert ciphertext.startswith(FERNET_CIPHERTEXT_PREFIX)

        monkeypatch.setenv(ENCRYPTION_KEY_ENV, key_b)
        assert decrypt_field(ciphertext) == ciphertext
        with pytest.raises(RuntimeError, match=ENCRYPTION_KEY_ENV):
            assert_plaintext_or_raise(ciphertext)

    def test_plaintext_and_correctly_keyed_ciphertext_pass(self, monkeypatch):
        key = Fernet.generate_key().decode()
        monkeypatch.setenv(ENCRYPTION_KEY_ENV, key)
        assert_plaintext_or_raise("ghp_genuine_plaintext_token")
        ciphertext = encrypt_field("some_secret")
        assert ciphertext is not None
        assert_plaintext_or_raise(ciphertext)

    def test_none_passes(self, monkeypatch):
        monkeypatch.setenv(ENCRYPTION_KEY_ENV, Fernet.generate_key().decode())
        assert_plaintext_or_raise(None)


class TestProbeKeyContinuity:
    """probe_key_continuity verifies or mints a per-environment continuity
    reference ciphertext."""

    def test_wrong_key_raises_on_probe(self, monkeypatch):
        """A syntactically valid but wrong Fernet key raises RuntimeError.

        Mint a reference with key_a, then verify with key_b (wrong key).
        probe_key_continuity must raise because the reference (minted under
        key_a) cannot be decrypted with key_b.
        """
        key_a = Fernet.generate_key()
        reference_ciphertext = Fernet(key_a).encrypt(
            CONTINUITY_REFERENCE_PLAINTEXT.encode()
        ).decode()

        key_b = Fernet.generate_key().decode()
        monkeypatch.setenv(ENCRYPTION_KEY_ENV, key_b)

        with pytest.raises(RuntimeError, match=ENCRYPTION_KEY_ENV):
            probe_key_continuity("test context", reference_ciphertext=reference_ciphertext)

    def test_correct_key_passes_continuity(self, monkeypatch):
        """The same key used to create the reference ciphertext passes the probe."""
        key_a = Fernet.generate_key()
        reference_ciphertext = Fernet(key_a).encrypt(
            CONTINUITY_REFERENCE_PLAINTEXT.encode()
        ).decode()
        monkeypatch.setenv(ENCRYPTION_KEY_ENV, key_a.decode())

        probe_key_continuity("test context", reference_ciphertext=reference_ciphertext)  # must not raise

    def test_no_key_configured_is_noop(self, monkeypatch):
        """When no key is configured, probe_key_continuity does nothing and returns None."""
        monkeypatch.delenv(ENCRYPTION_KEY_ENV, raising=False)
        monkeypatch.setenv(ALLOW_PLAINTEXT_CREDENTIALS_ENV, "true")
        result = probe_key_continuity("test context")  # must not raise
        assert result is None

    def test_error_message_names_env_var(self, monkeypatch):
        """RuntimeError message names GITHUB_PAT_ENCRYPTION_KEY for operator guidance."""
        key_a = Fernet.generate_key()
        reference_ciphertext = Fernet(key_a).encrypt(
            CONTINUITY_REFERENCE_PLAINTEXT.encode()
        ).decode()

        key_b = Fernet.generate_key().decode()
        monkeypatch.setenv(ENCRYPTION_KEY_ENV, key_b)

        with pytest.raises(RuntimeError) as exc_info:
            probe_key_continuity("startup", reference_ciphertext=reference_ciphertext)
        assert ENCRYPTION_KEY_ENV in str(exc_info.value)
        assert "startup" in str(exc_info.value)

    def test_first_boot_mints_reference(self, monkeypatch):
        """When no reference_ciphertext is passed and encryption is active,
        probe_key_continuity mints a new continuity reference (no raise)."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv(ENCRYPTION_KEY_ENV, key)

        result = probe_key_continuity("test context")
        assert result is not None
        # The minted reference must be decryptable with the same key.
        plaintext = Fernet(key.encode()).decrypt(result.encode()).decode()
        assert plaintext == CONTINUITY_REFERENCE_PLAINTEXT

    def test_fresh_env_any_valid_key_boots(self, monkeypatch):
        """A fresh environment with any valid Fernet key boots cleanly
        (no RuntimeError, reference is minted)."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv(ENCRYPTION_KEY_ENV, key)

        # Simulate first boot: no reference passed, probe mints one.
        result = probe_key_continuity("startup")
        assert result is not None

        # Simulate second boot: stored reference verified against same key.
        probe_key_continuity("startup", reference_ciphertext=result)  # must not raise
