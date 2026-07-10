"""Tests for the field-level encryption module ``app.field_encryption``.

Covers encrypt/decrypt round-trip with a valid Fernet key, None passthrough,
missing-key no-op behavior, and InvalidToken plaintext fallback.
"""

from cryptography.fernet import Fernet
from app.field_encryption import encrypt_field, decrypt_field


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
    """Both functions are no-ops when GITHUB_PAT_ENCRYPTION_KEY is not set."""

    def test_encrypt_noop(self, monkeypatch):
        """encrypt_field returns the value unchanged when key is absent."""
        monkeypatch.delenv("GITHUB_PAT_ENCRYPTION_KEY", raising=False)
        original = "plaintext_token"
        # Identity check: the function returns the exact same object, not a copy
        assert encrypt_field(original) is original

    def test_decrypt_noop(self, monkeypatch):
        """decrypt_field returns the value unchanged when key is absent."""
        monkeypatch.delenv("GITHUB_PAT_ENCRYPTION_KEY", raising=False)
        original = "plaintext_token"
        assert decrypt_field(original) is original

    def test_encrypt_empty_string_noop(self, monkeypatch):
        """encrypt_field('') returns '' (the same object) when key is absent."""
        monkeypatch.delenv("GITHUB_PAT_ENCRYPTION_KEY", raising=False)
        assert encrypt_field("") == ""

    def test_decrypt_empty_string_noop(self, monkeypatch):
        """decrypt_field('') returns '' (the same object) when key is absent."""
        monkeypatch.delenv("GITHUB_PAT_ENCRYPTION_KEY", raising=False)
        assert decrypt_field("") == ""


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
