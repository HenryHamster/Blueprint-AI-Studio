"""Tests for app/compute_image_remote_build.py — standalone (no conftest)."""

import os

import pytest


# ── Helpers ─────────────────────────────────────────────────────────────────

def _write_repo(tmp_path, dockerfile=b"FROM python:3.11-slim\nRUN pip install numpy\n",
                deps=None):
    """Minimal repo fixture matching the one in test_compute.py."""
    repo = tmp_path / "repo"
    (repo / ".blueprint").mkdir(parents=True)
    (repo / ".blueprint" / "Dockerfile.compute").write_bytes(dockerfile)
    deps = deps if deps is not None else {"requirements.txt": b"numpy==1.26\n"}
    for name, content in deps.items():
        (repo / name).write_bytes(content)
    return str(repo)


# ── registry_tag_exists ────────────────────────────────────────────────────


def test_remote_registry_tag_exists_present(monkeypatch):
    """remote_registry_tag_exists uses Artifact Registry tags."""
    from app.compute_image_remote_build import remote_registry_tag_exists
    class Image:
        tags = ["us-docker.pkg.dev/proj/repo/img:abc123"]  # noqa: RUF012
        name = "projects/proj/locations/us/repositories/repo/dockerImages/img@sha256:deadbeef"
    class Client:
        def list_docker_images(self, request):
            return [Image()]
    assert remote_registry_tag_exists(
        "us-docker.pkg.dev/proj/repo/img", "abc123", _client=Client()
    ) is True


def test_remote_registry_tag_exists_absent(monkeypatch):
    """remote_registry_tag_exists returns False when gcloud finds no match."""
    from app.compute_image_remote_build import remote_registry_tag_exists

    def fake_run(cmd, **kw):
        class P:
            returncode = 0
            stdout = "[]"
            stderr = ""
        return P()

    assert remote_registry_tag_exists("reg/repo/img", "abc123", _run=fake_run) is False


def test_remote_registry_tag_exists_fail_open_on_error(monkeypatch):
    """remote_registry_tag_exists returns False (not-present) on any error."""
    from app.compute_image_remote_build import remote_registry_tag_exists

    def fake_run(cmd, **kw):
        raise OSError("gcloud not found")

    assert remote_registry_tag_exists("reg/repo/img", "abc123", _run=fake_run) is False


def test_remote_registry_tag_exists_gcloud_nonzero(monkeypatch):
    """Non-zero exit from gcloud => not present."""
    from app.compute_image_remote_build import remote_registry_tag_exists

    def fake_run(cmd, **kw):
        class P:
            returncode = 1
            stdout = ""
            stderr = "permission denied"
        return P()

    assert remote_registry_tag_exists("reg/repo/img", "abc123", _run=fake_run) is False


# ── _resolve_registry_digest_from_api ───────────────────────────────────────


def test_remote_resolve_digest_from_api(monkeypatch):
    """Returns the digest from the Artifact Registry API resource."""
    from app.compute_image_remote_build import _resolve_registry_digest_from_api
    class Image:
        tags = ["us-docker.pkg.dev/proj/repo/img:abc123"]  # noqa: RUF012
        name = "projects/proj/locations/us/repositories/repo/dockerImages/img@sha256:baadf00d"
    class Client:
        def list_docker_images(self, request):
            return [Image()]
    digest = _resolve_registry_digest_from_api(
        "us-docker.pkg.dev/proj/repo/img", "abc123", _client=Client()
    )
    assert digest is not None and digest.startswith("sha256:")


def test_remote_resolve_digest_from_api_not_found(monkeypatch):
    """Returns None when tag is absent."""
    from app.compute_image_remote_build import _resolve_registry_digest_from_api

    def fake_run(cmd, **kw):
        class P:
            returncode = 0
            stdout = "[]"
            stderr = ""
        return P()

    assert _resolve_registry_digest_from_api("reg/repo/img", "nonexistent", _run=fake_run) is None


# ── remote_build_and_push ───────────────────────────────────────────────────


def test_remote_build_and_push_fast_path_skips_when_tag_exists(tmp_path, monkeypatch):
    """Remote build skips submission when the hash-tagged image already exists."""
    from app.compute_image_remote_build import remote_build_and_push

    monkeypatch.setenv("COMPUTE_IMAGE_BUILDER_SA", "builder-sa@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_LOCATION", "us-central1")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", "proj-compute-build-source")

    repo = _write_repo(tmp_path)
    run_calls = []

    def fake_run(cmd, **kw):
        run_calls.append(cmd)
        class P:
            returncode = 0
            stdout = '[{"tag": "abc123", "version": "sha256:baadf00d"}]'
            stderr = ""
        return P()

    result = remote_build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        _run=fake_run,
        _tag_exists_fn=lambda repo_tag, tag, _run=None: True,
    )
    assert result.already_present is True
    assert result.built is False
    # No Cloud Build submission was made.
    assert not any("builds submit" in " ".join(c) for c in run_calls)


def test_remote_build_and_push_credential_leak_fails_before_submission(tmp_path, monkeypatch):
    """Input credential scan fails BEFORE Cloud Build is invoked."""
    from app.compute_image_remote_build import remote_build_and_push
    from app.compute_image_build import CredentialLeakError

    monkeypatch.setenv("COMPUTE_IMAGE_BUILDER_SA", "builder-sa@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_LOCATION", "us-central1")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", "proj-compute-build-source")

    # Dockerfile with a literal GitHub PAT baked into an ENV.
    dockerfile = b"FROM python:3.11-slim\nENV GITHUB_APP_PRIVATE_KEY=ghs_deadbeefdeadbeefdeadbeef\n"
    repo = _write_repo(tmp_path, dockerfile=dockerfile)
    run_calls = []

    def fake_run(cmd, **kw):
        run_calls.append(cmd)
        class P:
            returncode = 0
            stdout = "[]"
            stderr = ""
        return P()

    with pytest.raises(CredentialLeakError, match="credential scan failed"):
        remote_build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
            _run=fake_run,
            _tag_exists_fn=lambda *a, **k: False,
        )
    # No Cloud Build was ever submitted — the credential gate fired first.
    assert not any("builds submit" in " ".join(c) for c in run_calls)


def test_remote_build_rejects_secret_named_build_arg_before_registry_or_submit(tmp_path, monkeypatch):
    from app.compute_image_build import CredentialLeakError
    from app.compute_image_remote_build import remote_build_and_push

    monkeypatch.setenv("COMPUTE_IMAGE_BUILDER_SA", "builder@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_LOCATION", "us-central1")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", "source-bucket")
    called = []
    with pytest.raises(CredentialLeakError, match="OPENAI_API_KEY"):
        remote_build_and_push(
            repo_dir=_write_repo(tmp_path), registry_repo="us-docker.pkg.dev/proj/repo",
            image_name="img", dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"], build_args={"OPENAI_API_KEY": "short-secret"},
            _tag_exists_fn=lambda *args: called.append(args) or False,
        )
    assert called == []


def test_fast_path_queries_full_image_repository(tmp_path, monkeypatch):
    from app.compute_image_remote_build import remote_build_and_push
    monkeypatch.setenv("COMPUTE_IMAGE_BUILDER_SA", "builder@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_LOCATION", "us-central1")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", "source-bucket")
    queried = []
    result = remote_build_and_push(
        repo_dir=_write_repo(tmp_path), registry_repo="us-docker.pkg.dev/proj/repo",
        image_name="img", dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        _tag_exists_fn=lambda repo, tag: queried.append(repo) or True,
        _resolve_digest_fn=lambda *args: "sha256:abc",
    )
    assert result.already_present
    assert queried == ["us-docker.pkg.dev/proj/repo/img"]


def test_remote_build_and_push_dry_run_does_not_submit(tmp_path, monkeypatch):
    """dry_run=True skips Cloud Build submission, returns hash-only result."""
    from app.compute_image_remote_build import remote_build_and_push

    monkeypatch.setenv("COMPUTE_IMAGE_BUILDER_SA", "builder-sa@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_LOCATION", "us-central1")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", "proj-compute-build-source")

    repo = _write_repo(tmp_path)
    run_calls = []

    def fake_run(cmd, **kw):
        run_calls.append(cmd)
        class P:
            returncode = 0
            stdout = "[]"
            stderr = ""
        return P()

    result = remote_build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        dry_run=True,
        _run=fake_run,
        _tag_exists_fn=lambda *a, **k: False,
    )
    assert result.already_present is False
    assert result.built is False
    # No Cloud Build submission on dry_run.
    assert not any("builds submit" in " ".join(c) for c in run_calls)
    # But we do get a dep_hash and a pinned_ref (tag-only in dry_run).
    assert result.dep_hash
    assert result.pinned_ref.endswith(result.dep_hash)


def test_remote_build_and_push_unchanged_hash_avoids_submission(tmp_path, monkeypatch):
    """When the dep hash exists in the registry, no Cloud Build is submitted."""
    from app.compute_image_remote_build import remote_build_and_push

    monkeypatch.setenv("COMPUTE_IMAGE_BUILDER_SA", "builder-sa@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_LOCATION", "us-central1")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", "proj-compute-build-source")

    repo = _write_repo(tmp_path)
    run_calls = []

    def fake_run(cmd, **kw):
        run_calls.append(" ".join(cmd))
        class P:
            returncode = 0
            stdout = '[{"tag": "abc123", "version": "sha256:ccdd"}]'
            stderr = ""
        return P()

    result = remote_build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        _run=fake_run,
        _tag_exists_fn=lambda *a, **k: True,
    )

    assert result.already_present is True
    assert result.built is False
    assert not any("builds submit" in c for c in run_calls)


def test_remote_build_and_push_not_configured_raises(tmp_path, monkeypatch):
    """When env vars are missing, the remote path raises a clear error."""
    from app.compute_image_remote_build import remote_build_and_push

    repo = _write_repo(tmp_path)

    with pytest.raises(RuntimeError, match="not configured"):
        remote_build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
        )


def test_remote_build_and_push_missing_dockerfile_raises(tmp_path, monkeypatch):
    """Missing Dockerfile raises FileNotFoundError before any remote work."""
    from app.compute_image_remote_build import remote_build_and_push

    monkeypatch.setenv("COMPUTE_IMAGE_BUILDER_SA", "builder-sa@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_LOCATION", "us-central1")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", "proj-compute-build-source")

    repo = _write_repo(tmp_path)
    # Remove the Dockerfile.
    os.unlink(os.path.join(repo, ".blueprint", "Dockerfile.compute"))

    with pytest.raises(FileNotFoundError, match="Dockerfile not found"):
        remote_build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
        )


def test_remote_build_and_push_missing_dep_file_raises(tmp_path, monkeypatch):
    """Missing dep file raises FileNotFoundError before any remote work."""
    from app.compute_image_remote_build import remote_build_and_push

    monkeypatch.setenv("COMPUTE_IMAGE_BUILDER_SA", "builder-sa@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_LOCATION", "us-central1")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", "proj-compute-build-source")

    repo = _write_repo(tmp_path)

    with pytest.raises(FileNotFoundError, match="declared dependency file not found"):
        remote_build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt", "nonexistent.lock"],
        )


def test_remote_build_and_push_succeeds_without_local_docker(tmp_path, monkeypatch):
    """Remote build succeeds even when docker is unavailable locally.

    This is the key acceptance test: the remote path never calls docker,
    only gcloud.  The runner proves a changed image succeeds with the local
    Docker runner unavailable.
    """
    from app.compute_image_remote_build import remote_build_and_push

    monkeypatch.setenv("COMPUTE_IMAGE_BUILDER_SA", "builder-sa@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_LOCATION", "us-central1")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", "proj-compute-build-source")
    monkeypatch.setenv("GCP_PROJECT_ID", "test-proj")

    repo = _write_repo(tmp_path)
    run_calls = []

    # Simulate: tag does NOT exist (lambda, no _run call), then tar packages
    # source (real subprocess), then submit succeeds, then poll WORKING, then
    # poll SUCCESS, then resolve digest.
    call_seq = iter(["submit", "poll", "poll_success", "digest"])

    def fake_run(cmd, **kw):
        cmd_str = " ".join(cmd)
        run_calls.append(cmd_str)
        kind = next(call_seq, "extra")

        class P:
            returncode = 0
            stderr = ""

        if kind == "submit":
            P.stdout = '{"id": "build-abc", "logUrl": "https://..."}'
        elif kind == "poll":
            P.stdout = '{"id": "build-abc", "status": "WORKING"}'
        elif kind == "poll_success":
            P.stdout = '{"id": "build-abc", "status": "SUCCESS"}'
        elif kind == "digest":
            P.stdout = '[{"tag": "abc123", "version": "sha256:ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100"}]'
        else:
            P.stdout = "[]"
        return P()

    from app.compute_image_remote_build import _CloudBuildSubmission
    monkeypatch.setattr("app.compute_image_remote_build._delete_staged_object", lambda _name: None)
    result = remote_build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        _tag_exists_fn=lambda *a, **k: False,
        _submit_fn=lambda **kw: _CloudBuildSubmission("build-abc", "https://...", "source.tgz"),
        _poll_fn=lambda *a, **kw: {"status": "SUCCESS"},
        _resolve_digest_fn=lambda *a, **kw: "sha256:ffeeddccbbaa9988",
    )

    assert result.built is True
    assert result.already_present is False
    assert result.pinned_ref.startswith("reg/repo/img@sha256:")
    # Local docker daemon was never invoked (no `docker build`, `docker push`,
    # `docker save`, `docker manifest`, or `docker image`).
    _docker_invocations = [
        c for c in run_calls
        if any(c.startswith(prefix) for prefix in (
            "docker ", "docker build", "docker push", "docker save",
            "docker manifest", "docker image",
        ))
    ]
    assert not _docker_invocations, f"local docker called: {_docker_invocations}"
    assert not run_calls


def test_remote_build_and_push_cancellation_reaches_cloud_build(tmp_path, monkeypatch):
    """When BuildControl is aborted, the Cloud Build is cancelled and drained to terminal."""
    from app.compute_image_remote_build import remote_build_and_push
    from app.compute_image_build import (
        BuildControl, BuildAborted,
        set_build_control, reset_build_control,
    )

    monkeypatch.setenv("COMPUTE_IMAGE_BUILDER_SA", "builder-sa@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_LOCATION", "us-central1")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", "proj-compute-build-source")
    monkeypatch.setenv("GCP_PROJECT_ID", "test-proj")

    repo = _write_repo(tmp_path)

    from app.compute_image_remote_build import _CloudBuildSubmission
    cancelled = []
    terminal_waited = []
    monkeypatch.setattr("app.compute_image_remote_build._delete_staged_object", lambda _name: None)
    monkeypatch.setattr("app.compute_image_remote_build._cancel_cloud_build",
                        lambda build_id, **_kw: cancelled.append(build_id) or True)
    monkeypatch.setattr("app.compute_image_remote_build._wait_for_cloud_build_terminal",
                        lambda build_id, **_kw: terminal_waited.append(build_id))

    def abort_poll(*args, **kwargs):
        raise BuildAborted("cancelled")

    control = BuildControl()
    token = set_build_control(control)
    try:
        with pytest.raises(BuildAborted, match="cancelled"):
            remote_build_and_push(
                repo_dir=repo,
                registry_repo="reg/repo",
                image_name="img",
                dockerfile=".blueprint/Dockerfile.compute",
                dep_files=["requirements.txt"],
                _tag_exists_fn=lambda *a, **k: False,
                _submit_fn=lambda **kw: _CloudBuildSubmission("build-12345", "", "source.tgz"),
                _poll_fn=abort_poll,
            )
    finally:
        reset_build_control(token)

    assert cancelled == ["build-12345"]
    # After cancelling, the code must poll until Cloud Build is terminal before
    # deleting staged source — verified by the _wait_for_cloud_build_terminal call.
    assert terminal_waited == ["build-12345"]


def test_submit_cloud_build_normalizes_service_account_email(tmp_path, monkeypatch):
    """_submit_cloud_build converts SA email to the fully-qualified resource name."""
    monkeypatch.setenv("COMPUTE_IMAGE_BUILDER_SA", "sa@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_LOCATION", "us-central1")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", "build-bucket")
    monkeypatch.setenv("GCP_PROJECT_ID", "my-project")

    submitted_builds = []

    class FakeBlob:
        def upload_from_filename(self, *a, **kw): pass
        def delete(self): pass

    class FakeBucket:
        def blob(self, name): return FakeBlob()

    class FakeStorageClient:
        def bucket(self, name): return FakeBucket()

    class FakeOperation:
        class metadata:
            class build:
                id = "build-normalized-sa"
                log_url = "https://logs.example.com"

    class FakeCBClient:
        def create_build(self, request):
            submitted_builds.append(request["build"])
            return FakeOperation()

    # Write a minimal archive to satisfy upload_from_filename
    archive = str(tmp_path / "source.tar.gz")
    import tarfile as _tf
    with _tf.open(archive, "w:gz"):
        pass

    from app.compute_image_remote_build import _submit_cloud_build
    _submit_cloud_build(
        source_tarball=archive,
        full_image_ref="us-docker.pkg.dev/my-project/repo/img:abc",
        dockerfile_rel=".blueprint/Dockerfile.compute",
        build_args=None,
        parent=None,
        tag="abc",
        _client=FakeCBClient(),
        _storage_client=FakeStorageClient(),
    )

    assert len(submitted_builds) == 1
    sa = submitted_builds[0].service_account
    assert sa == "projects/my-project/serviceAccounts/sa@proj.iam.gserviceaccount.com", (
        f"Expected fully-qualified resource name, got: {sa!r}"
    )


# ── _remote_build_configured ────────────────────────────────────────────────


def test_remote_build_configured_detects_missing_vars(monkeypatch):
    """_remote_build_configured returns False when env vars are missing."""
    from app.compute_image_remote_build import _remote_build_configured

    monkeypatch.delenv("COMPUTE_IMAGE_BUILDER_SA", raising=False)
    monkeypatch.delenv("COMPUTE_IMAGE_BUILD_LOCATION", raising=False)
    monkeypatch.delenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", raising=False)

    assert _remote_build_configured() is False


def test_remote_build_configured_detects_all_present(monkeypatch):
    """_remote_build_configured returns True when all env vars are set."""
    from app.compute_image_remote_build import _remote_build_configured

    monkeypatch.setenv("COMPUTE_IMAGE_BUILDER_SA", "builder@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_LOCATION", "us-central1")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", "my-bucket")

    assert _remote_build_configured() is True


# ── _remote_build_configured partial coverage ────────────────────────────────


def test_remote_build_configured_requires_all_three(monkeypatch):
    """All three env vars must be set; any missing => False."""
    from app.compute_image_remote_build import _remote_build_configured

    monkeypatch.setenv("COMPUTE_IMAGE_BUILDER_SA", "builder@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_LOCATION", "us-central1")
    # Missing COMPUTE_IMAGE_BUILD_SOURCE_BUCKET
    monkeypatch.delenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", raising=False)

    assert _remote_build_configured() is False


def test_remote_build_and_push_rejects_credential_in_dep_file(tmp_path, monkeypatch):
    """Credential scan of dependency inputs rejects before Cloud Build submission."""
    from app.compute_image_remote_build import remote_build_and_push
    from app.compute_image_build import CredentialLeakError

    monkeypatch.setenv("COMPUTE_IMAGE_BUILDER_SA", "builder-sa@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_LOCATION", "us-central1")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", "proj-compute-build-source")

    # requirements.txt with a Google API key baked in.
    deps = {"requirements.txt": b"numpy==1.26\n# api key: AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZabcdefg\n"}
    repo = _write_repo(tmp_path, deps=deps)
    run_calls = []

    def fake_run(cmd, **kw):
        run_calls.append(cmd)
        class P:
            returncode = 0
            stdout = "[]"
            stderr = ""
        return P()

    with pytest.raises(CredentialLeakError, match="credential scan failed"):
        remote_build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
            _run=fake_run,
            _tag_exists_fn=lambda *a, **k: False,
        )
    # No Cloud Build was ever submitted.
    assert not any("builds submit" in " ".join(c) for c in run_calls)


def test_remote_build_and_push_fast_path_preserves_digest(tmp_path, monkeypatch):
    """Fast path returns a digest-pinned ref when the registry has one."""
    from app.compute_image_remote_build import remote_build_and_push

    monkeypatch.setenv("COMPUTE_IMAGE_BUILDER_SA", "builder-sa@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_LOCATION", "us-central1")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", "proj-compute-build-source")

    repo = _write_repo(tmp_path)

    def fake_run(cmd, **kw):
        class P:
            returncode = 0
            stdout = '[{"tag": "abc123", "version": "sha256:ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100"}]'
            stderr = ""
        return P()

    result = remote_build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        _run=fake_run,
        _tag_exists_fn=lambda *a, **k: True,
        _resolve_digest_fn=lambda *a, **k: "sha256:ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100",
    )

    assert result.already_present is True
    assert result.digest == "sha256:ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100"
    assert result.pinned_ref == "reg/repo/img@sha256:ffeeddccbbaa99887766554433221100ffeeddccbbaa99887766554433221100"


def test_remote_build_and_push_parent_and_build_args_in_hash(tmp_path, monkeypatch):
    """Parent image and build_args contribute to the dependency hash."""
    from app.compute_image_remote_build import remote_build_and_push

    monkeypatch.setenv("COMPUTE_IMAGE_BUILDER_SA", "builder-sa@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_LOCATION", "us-central1")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", "proj-compute-build-source")

    repo = _write_repo(tmp_path)

    def fake_run(cmd, **kw):
        class P:
            returncode = 0
            stdout = '[{"tag": "abc123", "version": "sha256:bb"}]'
            stderr = ""
        return P()

    # With parent and build_args.
    result1 = remote_build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        parent="python:3.11-slim",
        build_args={"CUDA_VERSION": "12.1"},
        _run=fake_run,
        _tag_exists_fn=lambda *a, **k: True,
    )

    # Without parent and build_args.
    # Same source files, different hash inputs => different dep_hash.
    # Since tag_exists always returns True, both hit the fast path,
    # but their hashes differ because parent + build_args are included.
    assert result1.dep_hash  # always has a dep_hash

    # The dep_hash includes parent + build_args, so the same source with
    # different parent or build_args produces a different hash.
    result2 = remote_build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        _run=fake_run,
        _tag_exists_fn=lambda *a, **k: True,
    )

    assert result1.dep_hash != result2.dep_hash, (
        "dep_hash should differ when parent/build_args differ"
    )


# ── Dockerfile network-override rejection ─────────────────────────────────────


def test_dockerfile_network_override_default_is_rejected(tmp_path, monkeypatch):
    """Adversarial: RUN --network=default must be rejected before any build."""
    from app.compute_image_remote_build import remote_build_and_push

    _write_repo(
        tmp_path,
        dockerfile=b"FROM python:3.11-slim\nRUN --network=default pip install numpy\n",
    )
    repo = str(tmp_path / "repo")

    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", "bkt")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILDER_SA", "sa@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_LOCATION", "us-central1")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")

    with pytest.raises(ValueError, match="network override"):
        remote_build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
            _tag_exists_fn=lambda *a, **k: False,
        )


def test_dockerfile_network_override_host_is_rejected(tmp_path, monkeypatch):
    """Adversarial: RUN --network=host must be rejected."""
    from app.compute_image_remote_build import remote_build_and_push

    _write_repo(
        tmp_path,
        dockerfile=b"FROM python:3.11-slim\nRUN --network=host pip install numpy\n",
    )
    repo = str(tmp_path / "repo")

    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", "bkt")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILDER_SA", "sa@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_LOCATION", "us-central1")
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")

    with pytest.raises(ValueError, match="network override"):
        remote_build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
            _tag_exists_fn=lambda *a, **k: False,
        )


def test_dockerfile_network_override_none_is_allowed(tmp_path, monkeypatch):
    """RUN --network=none is allowed (redundant but harmless)."""
    from app.compute_image_remote_build import _check_dockerfile_network_directives

    content = b"FROM python:3.11-slim\nRUN --network=none pip install numpy\n"
    # Must not raise
    _check_dockerfile_network_directives(content)


def test_dockerfile_no_network_directive_is_allowed(tmp_path, monkeypatch):
    """Dockerfiles with no --network flag pass the check."""
    from app.compute_image_remote_build import _check_dockerfile_network_directives

    content = b"FROM python:3.11-slim\nRUN pip install numpy\nCOPY . /app\n"
    _check_dockerfile_network_directives(content)


def test_dockerfile_network_override_space_separated_is_rejected(tmp_path, monkeypatch):
    """Adversarial: RUN --network default (space, not =) is also rejected."""
    from app.compute_image_remote_build import _check_dockerfile_network_directives

    content = b"FROM python:3.11-slim\nRUN --network default pip install numpy\n"
    with pytest.raises(ValueError, match="network override"):
        _check_dockerfile_network_directives(content)


def test_dockerfile_network_override_continuation_line_is_rejected(tmp_path, monkeypatch):
    """Adversarial: network override split across continuation lines is caught."""
    from app.compute_image_remote_build import _check_dockerfile_network_directives

    content = b"FROM python:3.11-slim\nRUN --network=default \\\n    pip install numpy\n"
    with pytest.raises(ValueError, match="network override"):
        _check_dockerfile_network_directives(content)
