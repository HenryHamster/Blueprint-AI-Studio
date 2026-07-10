"""Field-level encryption for sensitive database fields using Fernet.

Provides ``encrypt_field`` / ``decrypt_field`` for any single-column value stored
in the database that should be encrypted at rest.  The encryption key is loaded
from the ``GITHUB_PAT_ENCRYPTION_KEY`` environment variable.

When no key is configured, both functions are no-ops (pass-through) so that local
development and test environments without a key still work — the column is stored
in plaintext.  In production the key MUST be set; the app logs a warning on every
operation when it is missing.

Decryption gracefully handles values that are already in plaintext (e.g. rows
that predate the encryption migration) by falling through to return the value
as-is.  This provides backward compatibility during a rolling deploy.
"""

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

ENCRYPTION_KEY_ENV = "GITHUB_PAT_ENCRYPTION_KEY"


def _get_fernet() -> Fernet | None:
    """Return a ``Fernet`` instance configured from ``GITHUB_PAT_ENCRYPTION_KEY``.

    Returns ``None`` when the environment variable is unset, which causes
    :func:`encrypt_field` / :func:`decrypt_field` to pass through values
    unchanged.  The key must be a valid Fernet key (32 url-safe base64 bytes),
    typically generated with ``cryptography.fernet.Fernet.generate_key()``.
    """
    key = os.getenv(ENCRYPTION_KEY_ENV)
    if not key:
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


def encrypt_field(value: str | None) -> str | None:
    """Encrypt *value* using Fernet symmetric encryption.

    Returns ``None`` when *value* is ``None``.
    Returns *value* unchanged when no encryption key is configured.
    """
    if value is None:
        return None
    f = _get_fernet()
    if f is None:
        return value
    return f.encrypt(value.encode()).decode()


def decrypt_field(value: str | None) -> str | None:
    """Decrypt *value* that was previously encrypted with :func:`encrypt_field`.

    Returns ``None`` when *value* is ``None``.
    Returns *value* unchanged when no encryption key is configured.
    If decryption fails (e.g. *value* is still in plaintext from before the
    encryption migration was applied), the value is returned as-is so that
    a rolling deploy does not break existing data.
    """
    if value is None:
        return None
    f = _get_fernet()
    if f is None:
        return value
    try:
        return f.decrypt(value.encode()).decode()
    except InvalidToken:
        # Value is either plaintext (pre-encryption) or corrupt — return as-is
        # so callers can still use it.  A corrupt value will ultimately fail
        # at the downstream secret-resolver (GCP Secret Manager).
        return value
    except Exception as e:
        logger.error("Unexpected decryption error: %s", e)
        return value
