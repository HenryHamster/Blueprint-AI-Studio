"""V4 signed-URL generation must survive Cloud Run's token-only credentials.

On Cloud Run the default service account is delivered as
``google.auth.compute_engine.credentials.Credentials`` — an OAuth access token
with no private key — so google-cloud-storage's *local* V4 signer raises
``AttributeError: you need a private key to sign credentials``. These tests pin
the regression: signing must fall back to the IAM Credentials ``signBlob`` API
(``service_account_email`` + ``access_token``) instead of crashing, which is what
caused the 236x ``AttributeError`` storm at ``storage.py`` on every compute-job
artifact upload.
"""

import uuid
from types import SimpleNamespace

import app.storage as storage


class _LocalSigningBlob:
    """A blob whose local signer works (a real private key is present)."""

    def __init__(self):
        self.calls = []

    def generate_signed_url(self, **kwargs):
        self.calls.append(kwargs)
        return "https://signed/local"


class _TokenOnlyBlob:
    """A blob that mirrors Cloud Run: local signing fails, IAM signing works."""

    def __init__(self):
        self.calls = []

    def generate_signed_url(self, **kwargs):
        self.calls.append(kwargs)
        if "access_token" not in kwargs:
            # Exactly what google-cloud-storage raises for token-only creds.
            raise AttributeError(
                "you need a private key to sign credentials.the credentials you "
                "are currently using <class 'google.auth.compute_engine."
                "credentials.Credentials'> just contains a token."
            )
        return "https://signed/via-iam"


_FAKE_IAM_CREDS = SimpleNamespace(
    service_account_email="orchestrator-sa@proj.iam.gserviceaccount.com",
    token="ya29.fake-access-token",
)


def test_local_signing_used_without_iam_roundtrip(monkeypatch):
    """When local signing works, the IAM signBlob path must not be touched."""

    def _must_not_call():
        raise AssertionError("IAM signer must not run when local signing succeeds")

    monkeypatch.setattr(storage, "_iam_signer_credentials", _must_not_call)
    blob = _LocalSigningBlob()

    url = storage._generate_v4_signed_url(blob, version="v4", method="GET")

    assert url == "https://signed/local"
    assert len(blob.calls) == 1
    assert "access_token" not in blob.calls[0]


def test_token_only_creds_fall_back_to_iam_signblob(monkeypatch):
    """Token-only creds (Cloud Run) must retry via IAM signBlob, not crash."""
    monkeypatch.setattr(
        storage, "_iam_signer_credentials", lambda: _FAKE_IAM_CREDS
    )
    blob = _TokenOnlyBlob()

    url = storage._generate_v4_signed_url(blob, version="v4", method="PUT")

    assert url == "https://signed/via-iam"
    # First attempt is local (no token); fallback supplies SA email + token.
    assert "access_token" not in blob.calls[0]
    assert blob.calls[1]["service_account_email"] == _FAKE_IAM_CREDS.service_account_email
    assert blob.calls[1]["access_token"] == _FAKE_IAM_CREDS.token
    # Original signing kwargs are preserved across the retry.
    assert blob.calls[1]["version"] == "v4"
    assert blob.calls[1]["method"] == "PUT"


async def test_compute_job_upload_url_survives_token_only_creds(monkeypatch):
    """End-to-end: the upload-URL helper that crashed 236x now returns a URL."""
    job_id = uuid.uuid4()
    blob = _TokenOnlyBlob()
    bucket = SimpleNamespace(blob=lambda name: blob)
    monkeypatch.setattr(
        storage.storage, "Client", lambda project=None: SimpleNamespace(
            bucket=lambda name: bucket
        )
    )
    monkeypatch.setattr(
        storage, "_iam_signer_credentials", lambda: _FAKE_IAM_CREDS
    )

    url = await storage.generate_compute_job_artifact_upload_signed_url(job_id)

    assert url == "https://signed/via-iam"


async def test_download_url_survives_token_only_creds(monkeypatch):
    """The operator artifact-download path has the same latent bug — fix both."""
    blob = _TokenOnlyBlob()
    bucket = SimpleNamespace(blob=lambda name: blob)
    monkeypatch.setattr(
        storage.storage, "Client", lambda project=None: SimpleNamespace(
            bucket=lambda name: bucket
        )
    )
    monkeypatch.setattr(
        storage, "_iam_signer_credentials", lambda: _FAKE_IAM_CREDS
    )

    uri = f"gs://{storage.ARTIFACT_BUCKET_NAME}/compute-jobs/{uuid.uuid4()}/artifacts/bundle.tar.gz"
    url = await storage.generate_download_signed_url(uri, expiration_minutes=60)

    assert url == "https://signed/via-iam"
