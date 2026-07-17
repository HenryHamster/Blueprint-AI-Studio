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
    ENCRYPTION_KEY_ENV,
    ALLOW_PLAINTEXT_CREDENTIALS_ENV,
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
    """Both functions return None when given None."""

    def test_encrypt_none_no_key(self):
        """encrypt_field(None) returns None when no key is configured."""
        assert encrypt_field(None) is None

    def test_encrypt_none_with_key(self, monkeypatch):
        """encrypt_field(None) returns None even with a valid key."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)
        assert encrypt_field(None) is None

    def test_decrypt_none_no_key(self):
        """decrypt_field(None) returns None when no key is configured."""
        assert decrypt_field(None) is None

    def test_decrypt_none_with_key(self, monkeypatch):
        """decrypt_field(None) returns None even with a valid key."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)
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

    def test_oauth_token_none_passes_through(self, monkeypatch):
        """None oauth_token (non-Anthropic/OAuth accounts) stays None."""
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("GITHUB_PAT_ENCRYPTION_KEY", key)

        assert encrypt_field(None) is None
        assert decrypt_field(None) is None

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
