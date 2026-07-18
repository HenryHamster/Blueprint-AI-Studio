"""Field-level encryption for sensitive database fields using Fernet.

Provides ``encrypt_field`` / ``decrypt_field`` for any single-column value stored
in the database that should be encrypted at rest.  The encryption key is loaded
from the ``GITHUB_PAT_ENCRYPTION_KEY`` environment variable.

When no key is configured, both functions raise ``RuntimeError`` by default to
prevent silent plaintext storage.  Local development and test environments can
opt into plaintext mode by setting ``ALLOW_PLAINTEXT_CREDENTIALS=true`` —
``encrypt_field`` and ``decrypt_field`` then pass through values unchanged.

Decryption gracefully handles values that are already in plaintext (e.g. rows
that predate the encryption migration) by falling through to return the value
as-is.  This provides backward compatibility during a rolling deploy.
"""

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

ENCRYPTION_KEY_ENV = "GITHUB_PAT_ENCRYPTION_KEY"
ALLOW_PLAINTEXT_CREDENTIALS_ENV = "ALLOW_PLAINTEXT_CREDENTIALS"

# All Fernet tokens are base64url-encoded and begin with this prefix after
# the version byte (0x80) is encoded. Any stored value starting with this
# string is almost certainly Fernet ciphertext, not plaintext.
FERNET_CIPHERTEXT_PREFIX = "gAAAAA"

# Plaintext sentinel used to mint per-environment continuity reference tokens.
# probe_key_continuity() encrypts this with the CURRENT Fernet key on first boot
# and stores the result; on subsequent boots it decrypts the stored reference
# and fails closed on InvalidToken (wrong-key rotation / misconfiguration).
CONTINUITY_REFERENCE_PLAINTEXT = "blueprint-field-encryption-continuity-v1"


def is_encryption_active() -> bool:
    """Return ``True`` if Fernet encryption is properly configured and active.

    Returns ``False`` when no encryption key is configured or the key is invalid,
    meaning :func:`encrypt_field` and :func:`decrypt_field` will either raise
    (when ``ALLOW_PLAINTEXT_CREDENTIALS`` is absent) or pass through values
    unchanged (when the opt-in flag is set for development).
    """
    return _get_fernet() is not None


def _get_fernet() -> Fernet | None:
    """Return a ``Fernet`` instance configured from ``GITHUB_PAT_ENCRYPTION_KEY``.

    Returns ``None`` when the environment variable is unset or the key is
    invalid.  The key must be a valid Fernet key (32 url-safe base64 bytes),
    typically generated with ``cryptography.fernet.Fernet.generate_key()``.

    Suppresses the per-call warning when ``ALLOW_PLAINTEXT_CREDENTIALS`` is
    explicitly set (the startup check in :mod:`app.main` handles the advisory
    message once per boot for development environments).
    """
    key = os.getenv(ENCRYPTION_KEY_ENV)
    if not key:
        if not os.getenv(ALLOW_PLAINTEXT_CREDENTIALS_ENV):
            logger.warning(
                "%s not set — field encryption is a no-op; "
                "sensitive column values will be stored in plaintext",
                ENCRYPTION_KEY_ENV,
            )
        return None
    try:
        return Fernet(key.encode())
    except Exception as e:
        logger.error("Invalid %s: %s", ENCRYPTION_KEY_ENV, e)
        return None


def require_encryption_active(context: str) -> None:
    """Fail closed if encryption is expected but not active.

    Distinguishes two failure modes that ``is_encryption_active()`` conflates:

    1. **Key configured but malformed** — always raises ``RuntimeError``, even
       when ``ALLOW_PLAINTEXT_CREDENTIALS`` is set. A mis-keyed environment
       must not silently stamp a backfill as complete while credentials remain
       plaintext.
    2. **Key absent and dev opt-in not set** — raises ``RuntimeError``. Production
       environments without an explicit key and without the plaintext opt-in are
       treated as mis-configured, not as a safe no-op.

    Only permits pass-through when ``ALLOW_PLAINTEXT_CREDENTIALS`` is explicitly
    set **and** no key is configured (local development / CI without encryption).
    """
    key = os.getenv(ENCRYPTION_KEY_ENV)
    if key:
        try:
            Fernet(key.encode())
        except Exception as exc:
            raise RuntimeError(
                f"{context}: {ENCRYPTION_KEY_ENV} is set but contains an invalid "
                f"Fernet key — encryption cannot proceed. "
                f"Supply a valid key generated with Fernet.generate_key(). "
                f"(Error: {exc})"
            ) from exc
        return
    if not os.getenv(ALLOW_PLAINTEXT_CREDENTIALS_ENV):
        raise RuntimeError(
            f"{context}: {ENCRYPTION_KEY_ENV} is not set and "
            f"{ALLOW_PLAINTEXT_CREDENTIALS_ENV} is not set — "
            f"refusing to proceed without confirmed encryption or explicit "
            f"plaintext development opt-in. "
            f"Set {ALLOW_PLAINTEXT_CREDENTIALS_ENV}=true for keyless dev environments."
        )


def _require_encryption_available():
    """Guard: raise ``RuntimeError`` if encryption would silently pass through.

    When no valid encryption key is configured **and** the development opt-in
    flag ``ALLOW_PLAINTEXT_CREDENTIALS`` is not set, the caller must not proceed
    with a silent no-op — credentials would be stored or returned in plaintext
    without the operator's explicit consent.
    """
    if is_encryption_active():
        return
    if not os.getenv(ALLOW_PLAINTEXT_CREDENTIALS_ENV):
        raise RuntimeError(
            f"{ENCRYPTION_KEY_ENV} is not available and "
            f"{ALLOW_PLAINTEXT_CREDENTIALS_ENV} is not set. "
            f"Set {ALLOW_PLAINTEXT_CREDENTIALS_ENV}=true for development "
            "environments without encryption, or configure a valid "
            f"{ENCRYPTION_KEY_ENV} for production use."
        )


def encrypt_field(value: str | None) -> str | None:
    """Encrypt *value* using Fernet symmetric encryption.

    Returns ``None`` when *value* is ``None``.
    Raises ``RuntimeError`` when no encryption key is configured and
    ``ALLOW_PLAINTEXT_CREDENTIALS`` is not set.
    Returns *value* unchanged when no key is configured **and** the opt-in
    flag is set (development mode).
    """
    if value is None:
        return None
    f = _get_fernet()
    if f is None:
        _require_encryption_available()
        return value
    return f.encrypt(value.encode()).decode()


def assert_plaintext_or_raise(value: str | None) -> None:
    """Reject ciphertext that the configured key cannot decrypt.

    ``decrypt_field`` deliberately returns undecryptable values unchanged for
    rolling-deploy compatibility. Backfill migrations must not mistake an
    unchanged Fernet token for plaintext and encrypt it a second time.
    """
    if value is None:
        return
    if value.startswith(FERNET_CIPHERTEXT_PREFIX) and decrypt_field(value) == value:
        raise RuntimeError(
            f"Wrong-key ciphertext detected: value starts with '{FERNET_CIPHERTEXT_PREFIX}' "
            f"but decrypt_field returned it unchanged — the wrong encryption key may be "
            f"configured. Check {ENCRYPTION_KEY_ENV}. Refusing to treat "
            "undecryptable ciphertext as plaintext."
        )


def probe_key_continuity(
    context: str,
    reference_ciphertext: str | None = None,
) -> str | None:
    """Verify or mint a per-environment continuity reference ciphertext.

    **When *reference_ciphertext* is provided** — verify that the currently
    configured Fernet key can decrypt it.  Raises ``RuntimeError`` on
    ``InvalidToken``, meaning the key was rotated without re-encrypting
    at-rest data (or the wrong key is configured), and any backfill re-run
    would silently store plaintext.

    **When *reference_ciphertext* is ``None``** — mint a new continuity
    reference ciphertext with the currently configured key and return it.
    The caller should persist the returned value so it can be passed back
    on subsequent boots.

    Returns ``None`` when no key is configured (encryption inactive);
    callers that require encryption should call
    ``require_encryption_active`` separately.
    """
    f = _get_fernet()
    if f is None:
        return None

    if reference_ciphertext is not None:
        # Verify an existing reference — fail closed on wrong key.
        try:
            f.decrypt(reference_ciphertext.encode())
        except InvalidToken as exc:
            raise RuntimeError(
                f"{context}: {ENCRYPTION_KEY_ENV} is set but cannot decrypt the "
                "persisted continuity reference token — the configured key does not "
                "match the canonical encryption key used for existing at-rest "
                "ciphertext. A wrong-key rotation will leave encrypted columns "
                "unreadable. Restore the correct key or re-encrypt all data before "
                "deploying."
            ) from exc
        return None
    else:
        # First boot — mint a continuity reference with the current key.
        return f.encrypt(CONTINUITY_REFERENCE_PLAINTEXT.encode()).decode()


def decrypt_field(value: str | None) -> str | None:
    """Decrypt *value* that was previously encrypted with :func:`encrypt_field`.

    Returns ``None`` when *value* is ``None``.
    Raises ``RuntimeError`` when no encryption key is configured and
    ``ALLOW_PLAINTEXT_CREDENTIALS`` is not set.
    Returns *value* unchanged when no key is configured **and** the opt-in
    flag is set (development mode).
    If decryption fails (e.g. *value* is still in plaintext from before the
    encryption migration was applied), the value is returned as-is so that
    a rolling deploy does not break existing data.
    """
    if value is None:
        return None
    f = _get_fernet()
    if f is None:
        _require_encryption_available()
        return value
    try:
        return f.decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, TypeError) as e:
        # InvalidToken: plaintext or corrupt value (pre-encryption backward compat).
        # ValueError/TypeError: encoding failures (UnicodeEncodeError on value.encode(),
        # UnicodeDecodeError on result .decode(), binascii.Error from malformed base64).
        # All other exceptions propagate so library API changes or truly unexpected
        # errors are not silently swallowed.
        logger.warning(
            "Decryption fallback for %s (%s): %s — value returned as-is; "
            "if %s is rotated, the boot-time continuity probe should have caught this",
            type(e).__name__,
            ENCRYPTION_KEY_ENV,
            e,
            ENCRYPTION_KEY_ENV,
        )
        return value
