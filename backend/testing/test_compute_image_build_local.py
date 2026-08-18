"""
Local (non-remote) container image build/publish substrate tests for app/compute.py.

Covers:
- manifest compute.image.build parsing and normalization
- dependency-hash computation
- credential scanner (no-platform-creds-in-layers security constraint)
- build script hash-check-before-build orchestration (build_and_push)
- viewable artifact collection and upload pipeline
- no-progress watchdog helpers
- checkpoint resume helpers
"""

import asyncio
import base64
import io
import json
import tarfile
import tracemalloc
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import os
import shlex
import shutil
import signal
import sys
import threading
import time
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

from app.time_utils import _utcnow
from app import compute
from app import compute_backends as compute_backends_module
from app import compute_jobs
from app import schemas
from app import storage
from app.compute_backends import (
    ComputeCostResult,
    GcpComputeBackend,
    ComputeLaunchResult,
    ComputeLaunchSpec,
    ComputeStatusResult,
)
from app.database import resolve_async_engine
from testing.conftest import TestingSessionLocal, USE_POSTGRES
from app.models import (
    ComputeJob,
    ComputeJobFile,
    ComputeJobOutcome,
    ComputeJobStatus,
    Project,
    ProjectComputeImage,
    Task,
    TaskStatus,
)
from testing._compute_test_helpers import (
    _make_compute_task,
    _make_tar_gz_bundle,
    _terminal_record_bytes,
)

# ===========================================================================
# Per-project compute image BUILD+PUBLISH substrate
#
# Covers (PRODUCE side of the images-first direction):
#   * manifest compute.image.build parsing + normalization
#   * dependency-hash computation (scripts/build_compute_image.compute_dependency_hash)
#   * credential scanner (no-platform-creds-in-layers security constraint)
#   * build script hash-check-before-build orchestration (build_and_push)
#   * backend orchestration (compute_backends.build_project_compute_image)
# ===========================================================================

# The reusable build/publish logic lives in a backend module (stdlib-only,
# self-contained) so the DB-backed suite can exercise it directly; the
# standalone scripts/build_compute_image.py CLI loads the same module by path.
from app import compute_image_build as _bci


# ---------------------------------------------------------------------------
# Manifest compute.image.build parsing
# ---------------------------------------------------------------------------


def test_manifest_compute_image_build_wellformed():
    from app.routers.admin_project_tools import (
        _validate_manifest_compute_image,
        _normalize_manifest_compute_image,
    )

    spec = {
        "name": "us-docker.pkg.dev/proj/compute-images/p:latest",
        "build": {
            "dockerfile": ".blueprint/Dockerfile.compute",
            "parent": "python:3.11-slim",
            "build_args": {"CUDA": "12.4", "PY": "3.11"},
        },
    }
    assert _validate_manifest_compute_image(spec) is None
    norm = _normalize_manifest_compute_image(spec)
    assert norm["name"] == "us-docker.pkg.dev/proj/compute-images/p:latest"
    assert norm["build"]["dockerfile"] == ".blueprint/Dockerfile.compute"
    assert norm["build"]["parent"] == "python:3.11-slim"
    assert norm["build"]["build_args"] == {"CUDA": "12.4", "PY": "3.11"}


def test_manifest_compute_image_build_defaults_when_fields_missing():
    """An empty build object defaults dockerfile and empty build_args."""
    from app.routers.admin_project_tools import (
        _validate_manifest_compute_image,
        _normalize_manifest_compute_image,
        _DEFAULT_COMPUTE_DOCKERFILE,
    )

    spec = {"name": "reg/repo/img:tag", "build": {}}
    assert _validate_manifest_compute_image(spec) is None
    norm = _normalize_manifest_compute_image(spec)
    assert norm["build"]["dockerfile"] == _DEFAULT_COMPUTE_DOCKERFILE
    assert norm["build"]["parent"] is None
    assert norm["build"]["build_args"] == {}


def test_manifest_compute_image_build_absent_is_none():
    """No build section -> normalized build is None (prebuilt-image project)."""
    from app.routers.admin_project_tools import _normalize_manifest_compute_image

    norm = _normalize_manifest_compute_image({"name": "reg/repo/img:tag"})
    assert norm["build"] is None


def test_manifest_compute_image_build_invalid_dockerfile_type():
    from app.routers.admin_project_tools import _validate_manifest_compute_image

    err = _validate_manifest_compute_image(
        {"name": "reg/img:t", "build": {"dockerfile": 123}}
    )
    assert err is not None
    assert "dockerfile" in err


def test_manifest_compute_image_build_dockerfile_traversal_rejected():
    from app.routers.admin_project_tools import _validate_manifest_compute_image

    err = _validate_manifest_compute_image(
        {"name": "reg/img:t", "build": {"dockerfile": "../etc/passwd"}}
    )
    assert err is not None
    assert "dockerfile" in err


def test_manifest_compute_image_build_invalid_parent_type():
    from app.routers.admin_project_tools import _validate_manifest_compute_image

    err = _validate_manifest_compute_image(
        {"name": "reg/img:t", "build": {"parent": ["python"]}}
    )
    assert err is not None
    assert "parent" in err


def test_manifest_compute_image_build_invalid_build_args_type():
    from app.routers.admin_project_tools import _validate_manifest_compute_image

    err = _validate_manifest_compute_image(
        {"name": "reg/img:t", "build": {"build_args": ["not", "a", "dict"]}}
    )
    assert err is not None
    assert "build_args" in err

    err2 = _validate_manifest_compute_image(
        {"name": "reg/img:t", "build": {"build_args": {"k": 5}}}
    )
    assert err2 is not None
    assert "build_args" in err2


def test_manifest_compute_image_build_not_a_dict():
    from app.routers.admin_project_tools import _validate_manifest_compute_image

    err = _validate_manifest_compute_image(
        {"name": "reg/img:t", "build": "notanobject"}
    )
    assert err is not None
    assert "build" in err


# ---------------------------------------------------------------------------
# Dependency-hash computation
# ---------------------------------------------------------------------------


def test_dependency_hash_same_inputs_same_hash():
    a = _bci.compute_dependency_hash({"requirements.txt": b"numpy==1\n", "Dockerfile": b"FROM x\n"})
    b = _bci.compute_dependency_hash({"Dockerfile": b"FROM x\n", "requirements.txt": b"numpy==1\n"})
    assert a == b
    assert len(a) == _bci.DEP_HASH_LEN


def test_dependency_hash_different_content_different_hash():
    base = {"requirements.txt": b"numpy==1\n", "Dockerfile": b"FROM x\n"}
    changed_dep = {"requirements.txt": b"numpy==2\n", "Dockerfile": b"FROM x\n"}
    changed_dockerfile = {"requirements.txt": b"numpy==1\n", "Dockerfile": b"FROM y\n"}
    h0 = _bci.compute_dependency_hash(base)
    assert h0 != _bci.compute_dependency_hash(changed_dep)
    assert h0 != _bci.compute_dependency_hash(changed_dockerfile)


def test_dependency_hash_filename_matters():
    """Renaming a dep file changes the hash even with identical content."""
    h1 = _bci.compute_dependency_hash({"requirements.txt": b"x", "Dockerfile": b"y"})
    h2 = _bci.compute_dependency_hash({"poetry.lock": b"x", "Dockerfile": b"y"})
    assert h1 != h2


def test_dependency_hash_empty_raises():
    with pytest.raises(ValueError):
        _bci.compute_dependency_hash({})


# ---------------------------------------------------------------------------
# assemble_fingerprint_inputs / bare_manifest_fallback_files
# ---------------------------------------------------------------------------


def test_assemble_fingerprint_inputs_parent_changes_hash():
    """Changing the parent image changes the fingerprint hash."""
    h1 = _bci.compute_dependency_hash(
        _bci.assemble_fingerprint_inputs(b"FROM x\n", {}, parent="python:3.11-slim")
    )
    h2 = _bci.compute_dependency_hash(
        _bci.assemble_fingerprint_inputs(b"FROM x\n", {}, parent="python:3.12-slim")
    )
    assert h1 != h2


def test_assemble_fingerprint_inputs_no_parent_differs_from_with_parent():
    """No parent and some parent produce different hashes."""
    h_no = _bci.compute_dependency_hash(
        _bci.assemble_fingerprint_inputs(b"FROM x\n", {})
    )
    h_yes = _bci.compute_dependency_hash(
        _bci.assemble_fingerprint_inputs(b"FROM x\n", {}, parent="python:3.11-slim")
    )
    assert h_no != h_yes


def test_assemble_fingerprint_inputs_build_args_order_independent():
    """build_args hash is stable regardless of insertion order."""
    h1 = _bci.compute_dependency_hash(
        _bci.assemble_fingerprint_inputs(
            b"FROM x\n", {}, build_args={"A": "1", "B": "2"}
        )
    )
    h2 = _bci.compute_dependency_hash(
        _bci.assemble_fingerprint_inputs(
            b"FROM x\n", {}, build_args={"B": "2", "A": "1"}
        )
    )
    assert h1 == h2


def test_assemble_fingerprint_inputs_build_args_value_changes_hash():
    """Changing a build-arg value changes the hash."""
    h1 = _bci.compute_dependency_hash(
        _bci.assemble_fingerprint_inputs(b"FROM x\n", {}, build_args={"CUDA": "12.4"})
    )
    h2 = _bci.compute_dependency_hash(
        _bci.assemble_fingerprint_inputs(b"FROM x\n", {}, build_args={"CUDA": "12.5"})
    )
    assert h1 != h2


def test_assemble_fingerprint_inputs_empty_build_args_same_as_none():
    """Empty build_args dict does not add a pseudo-key (same hash as no build_args)."""
    h_none = _bci.compute_dependency_hash(
        _bci.assemble_fingerprint_inputs(b"FROM x\n", {})
    )
    h_empty = _bci.compute_dependency_hash(
        _bci.assemble_fingerprint_inputs(b"FROM x\n", {}, build_args={})
    )
    assert h_none == h_empty


def test_assemble_fingerprint_inputs_nested_dep_file_changes_hash():
    """A declared dep file at a nested path changes the hash when its content changes."""
    h1 = _bci.compute_dependency_hash(
        _bci.assemble_fingerprint_inputs(
            b"FROM x\n",
            {"packages/api/requirements.txt": b"numpy==1.26\n"},
        )
    )
    h2 = _bci.compute_dependency_hash(
        _bci.assemble_fingerprint_inputs(
            b"FROM x\n",
            {"packages/api/requirements.txt": b"numpy==2.0\n"},
        )
    )
    assert h1 != h2


@pytest.mark.parametrize("reserved_name", ["Dockerfile", "__parent_image__", "__build_args__"])
def test_assemble_fingerprint_inputs_reserved_looking_dep_is_typed(reserved_name):
    """Valid dependency names cannot overwrite Dockerfile or metadata inputs."""
    base = _bci.assemble_fingerprint_inputs(
        b"FROM x\n",
        {reserved_name: b"one"},
        parent="python:3.11-slim",
        build_args={"CUDA": "12.4"},
    )
    changed = _bci.assemble_fingerprint_inputs(
        b"FROM x\n",
        {reserved_name: b"two"},
        parent="python:3.11-slim",
        build_args={"CUDA": "12.4"},
    )
    assert _bci.compute_dependency_hash(base) != _bci.compute_dependency_hash(changed)
    assert base["\x00dockerfile"] == b"FROM x\n"
    assert base["\x00parent_image"] == b"python:3.11-slim"
    assert base["\x00build_args"] == b'{"CUDA": "12.4"}'
    assert base[f"\x00dep:{reserved_name}"] == b"one"


def test_build_and_push_includes_parent_in_hash(tmp_path):
    """build_and_push uses assemble_fingerprint_inputs: parent affects the dep_hash."""
    (tmp_path / ".blueprint").mkdir()
    (tmp_path / ".blueprint" / "Dockerfile.compute").write_bytes(b"FROM scratch\n")
    (tmp_path / "requirements.txt").write_bytes(b"torch==2.3\n")

    seen_hashes: list[str] = []

    def fake_run(cmd, **kw):
        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    for p in ("python:3.11", "python:3.12"):
        result = _bci.build_and_push(
            repo_dir=str(tmp_path),
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
            parent=p,
            _run=fake_run,
            _tag_exists=lambda *a, **k: True,
        )
        seen_hashes.append(result.dep_hash)

    assert seen_hashes[0] != seen_hashes[1], "different parent must yield different dep_hash"


def test_bare_manifest_fallback_files_includes_package_json_when_no_lockfile():
    """package.json is a candidate when no node lockfile is in the resolved set."""
    result = _bci.bare_manifest_fallback_files(frozenset())
    assert "package.json" in result


def test_bare_manifest_fallback_files_excludes_package_json_when_lockfile_present():
    """package.json is NOT a candidate when a node lockfile is in the resolved set."""
    for lockfile in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml"):
        result = _bci.bare_manifest_fallback_files(frozenset([lockfile]))
        assert "package.json" not in result, f"package.json must be skipped when {lockfile} present"


def test_bare_manifest_fallback_files_includes_pipfile_when_no_lock():
    result = _bci.bare_manifest_fallback_files(frozenset())
    assert "Pipfile" in result


def test_bare_manifest_fallback_files_excludes_pipfile_when_lock_present():
    result = _bci.bare_manifest_fallback_files(frozenset(["Pipfile.lock"]))
    assert "Pipfile" not in result


def test_bare_manifest_fallback_files_all_lockfiles_present_returns_nothing():
    """When every ecosystem has a lockfile, no bare manifests are candidates."""
    all_lockfiles = frozenset([
        "package-lock.json",
        "Pipfile.lock",
        "requirements.txt",
        "Cargo.lock",
        "go.sum",
        "Gemfile.lock",
        "gradle.lockfile",
        "composer.lock",
        "mix.lock",
        "pubspec.lock",
        "Package.resolved",
        "Manifest.toml",
        "stack.yaml.lock",
    ])
    result = _bci.bare_manifest_fallback_files(all_lockfiles)
    assert result == []


def test_bare_manifest_fallback_rust_no_lock():
    """Cargo.toml is a candidate when Cargo.lock is absent."""
    result = _bci.bare_manifest_fallback_files(frozenset())
    assert "Cargo.toml" in result


def test_bare_manifest_fallback_rust_excluded_when_lockfile_present():
    """Cargo.toml is NOT a candidate when Cargo.lock is present."""
    result = _bci.bare_manifest_fallback_files(frozenset(["Cargo.lock"]))
    assert "Cargo.toml" not in result


def test_bare_manifest_fallback_go_no_sum():
    """go.mod is a candidate when go.sum is absent."""
    result = _bci.bare_manifest_fallback_files(frozenset())
    assert "go.mod" in result


def test_bare_manifest_fallback_go_excluded_when_sum_present():
    """go.mod is NOT a candidate when go.sum is present."""
    result = _bci.bare_manifest_fallback_files(frozenset(["go.sum"]))
    assert "go.mod" not in result


def test_bare_manifest_fallback_ruby_no_lock():
    """Gemfile is a candidate when Gemfile.lock is absent."""
    result = _bci.bare_manifest_fallback_files(frozenset())
    assert "Gemfile" in result


def test_bare_manifest_fallback_ruby_excluded_when_lockfile_present():
    """Gemfile is NOT a candidate when Gemfile.lock is present."""
    result = _bci.bare_manifest_fallback_files(frozenset(["Gemfile.lock"]))
    assert "Gemfile" not in result


def test_bare_manifest_fallback_jvm_no_lockfile():
    """pom.xml/build.gradle/build.gradle.kts are candidates when gradle.lockfile is absent."""
    result = _bci.bare_manifest_fallback_files(frozenset())
    assert "pom.xml" in result
    assert "build.gradle" in result
    assert "build.gradle.kts" in result


def test_bare_manifest_fallback_jvm_excluded_when_gradle_lockfile_present():
    """JVM bare manifests are NOT candidates when gradle.lockfile is present."""
    result = _bci.bare_manifest_fallback_files(frozenset(["gradle.lockfile"]))
    assert "pom.xml" not in result
    assert "build.gradle" not in result
    assert "build.gradle.kts" not in result


def test_bare_manifest_fallback_php_no_lock():
    """composer.json is a candidate when composer.lock is absent."""
    result = _bci.bare_manifest_fallback_files(frozenset())
    assert "composer.json" in result


def test_bare_manifest_fallback_php_excluded_when_lockfile_present():
    """composer.json is NOT a candidate when composer.lock is present."""
    result = _bci.bare_manifest_fallback_files(frozenset(["composer.lock"]))
    assert "composer.json" not in result


def test_default_dep_files_includes_rust_go_ruby_jvm_php():
    """_DEFAULT_DEP_FILES covers all newly added ecosystem lockfiles."""
    from app.compute_backends import _DEFAULT_DEP_FILES

    assert "Cargo.lock" in _DEFAULT_DEP_FILES
    assert "go.sum" in _DEFAULT_DEP_FILES
    assert "Gemfile.lock" in _DEFAULT_DEP_FILES
    assert "gradle.lockfile" in _DEFAULT_DEP_FILES
    assert "pom.xml" in _DEFAULT_DEP_FILES
    assert "build.gradle" in _DEFAULT_DEP_FILES
    assert "build.gradle.kts" in _DEFAULT_DEP_FILES
    assert "composer.lock" in _DEFAULT_DEP_FILES


def test_default_dep_files_includes_long_tail_ecosystems():
    """_DEFAULT_DEP_FILES covers long-tail ecosystem lockfiles (Elixir/Dart/Swift/.NET/Julia/Scala/Haskell)."""
    from app.compute_backends import _DEFAULT_DEP_FILES

    assert "mix.lock" in _DEFAULT_DEP_FILES
    assert "pubspec.lock" in _DEFAULT_DEP_FILES
    assert "packages.lock.json" in _DEFAULT_DEP_FILES
    assert "Package.resolved" in _DEFAULT_DEP_FILES
    assert "Manifest.toml" in _DEFAULT_DEP_FILES
    assert "build.sbt" in _DEFAULT_DEP_FILES
    assert "stack.yaml.lock" in _DEFAULT_DEP_FILES
    assert "cabal.project.freeze" in _DEFAULT_DEP_FILES


def test_long_tail_lockfile_changes_hash(tmp_path):
    """mix.lock/pubspec.lock/packages.lock.json/Package.resolved/Manifest.toml trigger a rebuild.

    Acceptance: changing one of these lockfiles in a project with no other lockfile
    yields a different compute_dependency_hash; an unrelated file change does not.
    """
    from app.compute_backends import _discover_dep_files

    lockfiles = [
        ("mix.lock", b"# mix lock v1\n", b"# mix lock v2\n"),
        ("pubspec.lock", b"# pubspec lock v1\n", b"# pubspec lock v2\n"),
        ("packages.lock.json", b'{"v1": 1}\n', b'{"v2": 2}\n'),
        ("Package.resolved", b'{"v1": 1}\n', b'{"v2": 2}\n'),
        ("Manifest.toml", b"# manifest v1\n", b"# manifest v2\n"),
    ]
    for filename, content_a, content_b in lockfiles:
        p = tmp_path / filename
        p.write_bytes(content_a)
        files_a = _discover_dep_files(str(tmp_path))
        assert filename in files_a, f"_discover_dep_files missed {filename}"

        h_a = _bci.compute_dependency_hash(
            _bci.assemble_fingerprint_inputs(b"FROM x\n", {filename: content_a})
        )
        h_b = _bci.compute_dependency_hash(
            _bci.assemble_fingerprint_inputs(b"FROM x\n", {filename: content_b})
        )
        assert h_a != h_b, f"hash did not change when {filename} changed"

        p.unlink()


def test_discover_dep_files_rust_project(tmp_path):
    """_discover_dep_files returns Cargo.lock when present in a Rust project."""
    from app.compute_backends import _discover_dep_files

    (tmp_path / "Cargo.lock").write_bytes(b"# Cargo lock\n")
    (tmp_path / "Cargo.toml").write_bytes(b"[package]\nname = \"foo\"\n")

    files = _discover_dep_files(str(tmp_path))
    assert "Cargo.lock" in files
    # Cargo.toml is the bare-manifest fallback; when Cargo.lock is present it should NOT appear
    assert "Cargo.toml" not in files


def test_discover_dep_files_rust_fallback_to_manifest(tmp_path):
    """_discover_dep_files returns Cargo.toml when Cargo.lock is absent."""
    from app.compute_backends import _discover_dep_files

    (tmp_path / "Cargo.toml").write_bytes(b"[package]\nname = \"foo\"\n")

    files = _discover_dep_files(str(tmp_path))
    assert "Cargo.toml" in files
    assert "Cargo.lock" not in files


def test_bare_manifest_lockfile_less_content_changes_hash():
    """A bare manifest's content changes the fingerprint hash when no lockfile is present."""
    # package.json present, no node lockfile => package.json is a hash input
    h1 = _bci.compute_dependency_hash(
        _bci.assemble_fingerprint_inputs(
            b"FROM x\n",
            {"package.json": b'{"name": "my-app", "version": "1.0.0"}\n'},
        )
    )
    h2 = _bci.compute_dependency_hash(
        _bci.assemble_fingerprint_inputs(
            b"FROM x\n",
            {"package.json": b'{"name": "my-app", "version": "2.0.0"}\n'},
        )
    )
    assert h1 != h2


# ---------------------------------------------------------------------------
# Manifest compute.image.build: deps field
# ---------------------------------------------------------------------------


def test_manifest_compute_image_build_deps_valid():
    from app.routers.admin_project_tools import (
        _validate_manifest_compute_image,
        _normalize_manifest_compute_image,
    )

    spec = {
        "name": "reg/img:tag",
        "build": {"deps": ["packages/api/requirements.txt", "shared/pyproject.toml"]},
    }
    assert _validate_manifest_compute_image(spec) is None
    norm = _normalize_manifest_compute_image(spec)
    assert norm["build"]["deps"] == [
        "packages/api/requirements.txt",
        "shared/pyproject.toml",
    ]


def test_manifest_compute_image_build_deps_empty_list():
    from app.routers.admin_project_tools import (
        _validate_manifest_compute_image,
        _normalize_manifest_compute_image,
    )

    spec = {"name": "reg/img:tag", "build": {"deps": []}}
    assert _validate_manifest_compute_image(spec) is None
    norm = _normalize_manifest_compute_image(spec)
    assert norm["build"]["deps"] == []


def test_manifest_compute_image_build_deps_absent_defaults_empty():
    from app.routers.admin_project_tools import _normalize_manifest_compute_image

    norm = _normalize_manifest_compute_image({"name": "reg/img:tag", "build": {}})
    assert norm["build"]["deps"] == []


def test_manifest_compute_image_build_deps_traversal_rejected():
    from app.routers.admin_project_tools import _validate_manifest_compute_image

    err = _validate_manifest_compute_image(
        {"name": "reg/img:t", "build": {"deps": ["../etc/passwd"]}}
    )
    assert err is not None
    assert "deps" in err


def test_manifest_compute_image_build_deps_absolute_path_rejected():
    from app.routers.admin_project_tools import _validate_manifest_compute_image

    err = _validate_manifest_compute_image(
        {"name": "reg/img:t", "build": {"deps": ["/etc/secret"]}}
    )
    assert err is not None
    assert "deps" in err


def test_manifest_compute_image_build_deps_whitespace_absolute_path_rejected():
    from app.routers.admin_project_tools import _validate_manifest_compute_image

    err = _validate_manifest_compute_image(
        {"name": "reg/img:t", "build": {"deps": [" /etc/passwd"]}}
    )
    assert err is not None
    assert "deps" in err


@pytest.mark.parametrize(
    "path",
    ["C:\\secrets\\token", "C:/secrets/token", "\\secrets\\token", "\\\\server\\share\\token", "//server/share/token", "lib\\..\\secret"],
)
def test_manifest_compute_image_build_deps_non_repo_relative_paths_rejected(path):
    from app.routers.admin_project_tools import _validate_manifest_compute_image

    err = _validate_manifest_compute_image(
        {"name": "reg/img:t", "build": {"deps": [path]}}
    )
    assert err is not None
    assert "deps" in err


def test_manifest_compute_image_build_deps_non_string_rejected():
    from app.routers.admin_project_tools import _validate_manifest_compute_image

    err = _validate_manifest_compute_image(
        {"name": "reg/img:t", "build": {"deps": [123]}}
    )
    assert err is not None
    assert "deps" in err


def test_manifest_compute_image_build_deps_not_list_rejected():
    from app.routers.admin_project_tools import _validate_manifest_compute_image

    err = _validate_manifest_compute_image(
        {"name": "reg/img:t", "build": {"deps": "requirements.txt"}}
    )
    assert err is not None
    assert "deps" in err


# ---------------------------------------------------------------------------
# Credential scanner
# ---------------------------------------------------------------------------


def test_credential_scanner_flags_known_patterns():
    findings = _bci.scan_inputs_for_credentials(
        {
            "config": b"token = ghs_abcdefghijklmnopqrstuvwxyz0123",
            "keys": b"AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ01234567",
            "sa.json": b'{"type": "service_account", "project_id": "x"}',
        }
    )
    joined = " ".join(findings)
    assert "config" in joined and "github" in joined
    assert "google_api_key" in joined
    assert "gcp_sa_json" in joined


def test_credential_scanner_flags_secret_env_in_dockerfile():
    findings = _bci.scan_inputs_for_credentials(
        {"Dockerfile": b"FROM python:3.11\nENV ORCHESTRATOR_TOKEN=abc123secret\nRUN pip install x"},
        dockerfile_names=["Dockerfile"],
    )
    assert any("ORCHESTRATOR_TOKEN" in f for f in findings)


def test_credential_scanner_flags_secret_path_copy():
    findings = _bci.scan_inputs_for_credentials(
        {"Dockerfile": b"FROM x\nCOPY /secrets/token.txt /app/token\n"},
        dockerfile_names=["Dockerfile"],
    )
    assert any("secret path" in f for f in findings)


def test_credential_scanner_no_false_positive_on_benign_env():
    """Benign env vars and normal deps do not trip the scanner."""
    findings = _bci.scan_inputs_for_credentials(
        {
            "Dockerfile": b"FROM python:3.11\nENV PYTHONUNBUFFERED=1\nENV LANG=C.UTF-8\nENV PATH=/usr/bin\n",
            "requirements.txt": b"numpy==1.26.0\ntorch==2.3.0\nscipy==1.13\n",
        },
        dockerfile_names=["Dockerfile"],
    )
    assert findings == []


def test_credential_scanner_allows_env_var_reference():
    """ENV pinned to a $VAR reference (build-arg indirection) is not a leak."""
    findings = _bci.scan_inputs_for_credentials(
        {"Dockerfile": b"FROM x\nENV ANTHROPIC_API_KEY=$SOME_BUILD_ARG\n"},
        dockerfile_names=["Dockerfile"],
    )
    assert findings == []


def test_image_scanner_covers_runner_credential_allowlist():
    """Drift guard: every provider-credential key in runner_isolation is caught by the image scanner.

    runner_isolation.AGENT_ENV_ALLOWLIST is the canonical list of credential-bearing env
    keys the platform recognises. The image scanner's _SECRET_ENV_NAMES must cover all of
    them (plus FORBIDDEN_ENV_KEYS) so a key added to the runner cannot silently be baked
    into a published image layer. If this test fails, add the key to _SECRET_ENV_NAMES in
    backend/app/compute_image_build.py.
    """
    import importlib.util
    import pathlib
    import sys

    if sys.version_info < (3, 11):
        pytest.skip("runner_isolation.py uses Python 3.11 dataclass syntax; covered in CI")

    # Load runner_isolation by path (it is stdlib-only, not in the backend package).
    # Resolve across local + Docker test envs: scripts/test-backend.sh and CI both
    # mount backend/ at /app and the repo root at /repo, so a path relative to
    # __file__ alone cannot reach the repo root. Reuse the canonical resolver.
    from testing.test_runner_isolation_gpu_markers import _require_runner_isolation_path

    spec = importlib.util.spec_from_file_location(
        "runner_isolation",
        pathlib.Path(_require_runner_isolation_path()),
    )
    ri = importlib.util.module_from_spec(spec)
    # runner_isolation defines dataclasses; @dataclass resolves annotations via
    # sys.modules[cls.__module__], so the module must be registered BEFORE exec.
    # Restore (not just pop) any pre-existing entry: other tests import
    # runner_isolation from scripts/ and cache local_runner bound to that module
    # object — dropping the entry makes a later `import runner_isolation` create
    # a fresh module that local_runner.iso no longer points at, so their
    # patch.object calls become inert (order-dependent cross-test pollution).
    prior = sys.modules.get(spec.name)
    sys.modules[spec.name] = ri
    try:
        spec.loader.exec_module(ri)
    finally:
        if prior is not None:
            sys.modules[spec.name] = prior
        else:
            sys.modules.pop(spec.name, None)

    # Credential-bearing subset of AGENT_ENV_ALLOWLIST: keys whose names suggest they
    # carry a secret (API key, token, credentials, secret).  Config/routing/locale keys
    # (PROVIDER, LANG, TERM, …) are intentionally excluded — they carry no secrets.
    _CREDENTIAL_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_CREDENTIALS")
    credential_keys = {
        k for k in ri.AGENT_ENV_ALLOWLIST
        if any(k.upper().endswith(sfx) for sfx in _CREDENTIAL_SUFFIXES)
    }
    # FORBIDDEN_ENV_KEYS must also be detected — they must never reach task code at all.
    all_keys_to_cover = credential_keys | set(ri.FORBIDDEN_ENV_KEYS)

    secret_names = _bci._SECRET_ENV_NAMES

    # 1. Every credential key is in _SECRET_ENV_NAMES.
    missing = all_keys_to_cover - secret_names
    assert not missing, (
        f"runner_isolation keys not covered by compute_image_build._SECRET_ENV_NAMES: {sorted(missing)}. "
        "Add them to _SECRET_ENV_NAMES in backend/app/compute_image_build.py."
    )

    # 2. Each key is actually detected when literally assigned in a Dockerfile ENV.
    for key in sorted(all_keys_to_cover):
        dockerfile = f"FROM python:3.11-slim\nENV {key}=supersecretvalue\n".encode()
        findings = _bci.scan_inputs_for_credentials(
            {"Dockerfile": dockerfile},
            dockerfile_names=["Dockerfile"],
        )
        assert any(key in f for f in findings), (
            f"scan_inputs_for_credentials did not flag 'ENV {key}=...' — "
            "the key is in _SECRET_ENV_NAMES but the scanner did not detect it"
        )

    # 3. A clean Dockerfile with only benign vars produces no findings.
    clean = b"FROM python:3.11-slim\nENV PYTHONUNBUFFERED=1\nENV LANG=C.UTF-8\n"
    assert _bci.scan_inputs_for_credentials({"Dockerfile": clean}, dockerfile_names=["Dockerfile"]) == []


# ---------------------------------------------------------------------------
# Build script hash-check-before-build orchestration
# ---------------------------------------------------------------------------


def _write_repo(tmp_path, dockerfile=b"FROM python:3.11-slim\nRUN pip install numpy\n",
                deps=None):
    repo = tmp_path / "repo"
    (repo / ".blueprint").mkdir(parents=True)
    (repo / ".blueprint" / "Dockerfile.compute").write_bytes(dockerfile)
    deps = deps if deps is not None else {"requirements.txt": b"numpy==1.26\n"}
    for name, content in deps.items():
        (repo / name).write_bytes(content)
    return str(repo)


def test_build_and_push_skips_when_image_present(tmp_path):
    """When the hash-tagged image already exists, no build/push happens."""
    repo = _write_repo(tmp_path)
    run_calls = []

    def fake_run(cmd, **kw):
        run_calls.append(cmd)

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        # The already-present fast path reads the digest daemon-free from the
        # registry via `gcloud artifacts docker images describe --format=json`.
        if cmd[:5] == ["gcloud", "artifacts", "docker", "images", "describe"]:
            P.stdout = json.dumps({"image_summary": {"digest": "sha256:" + "a" * 64}})
        return P()

    result = _bci.build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        _run=fake_run,
        _tag_exists=lambda repo_repo, tag, _run=None: True,
    )
    assert result.already_present is True
    assert result.built is False
    # No `docker build` / `docker push` was invoked.
    assert not any("build" in c for c in run_calls)
    assert not any("push" in c for c in run_calls)
    assert result.pinned_ref == "reg/repo/img@sha256:" + "a" * 64


def test_build_and_push_builds_when_hash_new(tmp_path):
    """A new dependency hash triggers build + push and returns a pinned ref."""
    repo = _write_repo(tmp_path)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)

        class P:
            returncode = 0
            stderr = ""
            stdout = ""

        # After push, the canonical digest is read daemon-free from the registry
        # via `gcloud artifacts docker images describe --format=json` (#15569).
        if cmd[:5] == ["gcloud", "artifacts", "docker", "images", "describe"]:
            P.stdout = json.dumps({"image_summary": {"digest": "sha256:" + "b" * 64}})
        return P()

    result = _bci.build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        parent="python:3.11-slim",
        _run=fake_run,
        _tag_exists=lambda repo_repo, tag, _run=None: False,
        _scan_layers_fn=lambda *a, **k: [],  # no-op layer scan in unit test
    )
    assert result.built is True
    assert result.already_present is False
    flat = [" ".join(c) for c in cmds]
    assert any(c.startswith("docker build") for c in flat)
    assert any(c.startswith("docker push") for c in flat)
    # The declared parent is threaded through as a build-arg.
    assert any("PARENT_IMAGE=python:3.11-slim" in c for c in flat)
    assert result.pinned_ref == "reg/repo/img@sha256:" + "b" * 64


def test_build_and_push_sanitizes_authenticated_git_metadata_from_context(tmp_path):
    """A Dockerfile that encodes its whole context cannot recover Git credentials."""
    repo = _write_repo(
        tmp_path,
        dockerfile=(
            b"FROM scratch\nCOPY . /project\n"
            b"RUN find /project -type f -exec base64 {} \\;\n"
        ),
    )
    token = "ghs_abcdefghijklmnopqrstuvwxyz1234567890"
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir()
    (repo_path / ".git" / "config").write_text(
        f"[credential]\n\ttoken = {token}\n"
    )
    (repo_path / "vendor").mkdir()
    (repo_path / "vendor" / ".git").mkdir()
    (repo_path / "vendor" / ".git" / "config").write_text(token)
    (repo_path / "project-source.txt").write_text("ordinary project source\n")
    os.symlink("project-source.txt", repo_path / "project-source-link")
    contexts = []

    def fake_run(cmd, **kw):
        if cmd[:2] == ["docker", "build"]:
            context = cmd[-1]
            contexts.append(context)
            assert cmd[cmd.index("-f") + 1] == os.path.join(
                context, ".blueprint", "Dockerfile.compute"
            )
            assert not os.path.exists(os.path.join(context, ".git"))
            assert not os.path.exists(os.path.join(context, "vendor", ".git"))
            with open(os.path.join(context, "project-source.txt"), "rb") as fh:
                assert fh.read() == b"ordinary project source\n"
            assert os.path.islink(os.path.join(context, "project-source-link"))
            copied_files = []
            for root, _dirs, files in os.walk(context):
                for name in files:
                    with open(os.path.join(root, name), "rb") as copied_file:
                        copied_files.append(copied_file.read())
            # This simulates the Dockerfile's base64 command over every copied file.
            encoded_context = base64.b64encode(b"\0".join(copied_files))
            assert token.encode() not in base64.b64decode(encoded_context)
        return _ok_proc(stdout="reg/repo/img@sha256:" + "c" * 64)

    _bci.build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        _run=fake_run,
        _tag_exists=lambda *a, **k: False,
        _scan_layers_fn=lambda *a, **k: [],
    )

    assert len(contexts) == 1
    assert not os.path.exists(contexts[0]), "temporary Docker context was not cleaned up"


def test_build_and_push_credential_leak_fails_before_push(tmp_path):
    """A leaked credential in the Dockerfile fails the build BEFORE any push."""
    repo = _write_repo(
        tmp_path,
        dockerfile=b"FROM python:3.11\nENV RUNNER_TOKEN=supersecretvalue\n",
    )
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)

        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    with pytest.raises(_bci.CredentialLeakError):
        _bci.build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
            _run=fake_run,
            _tag_exists=lambda repo_repo, tag, _run=None: False,
        )
    # Nothing was pushed.
    assert not any("push" in c for c in cmds)


def test_build_and_push_rejects_secret_build_args_before_build(tmp_path):
    """A platform-secret key in build_args is rejected before docker build runs."""
    repo = _write_repo(tmp_path)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)

        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    with pytest.raises(_bci.CredentialLeakError, match="build_args contains platform secret keys"):
        _bci.build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
            build_args={"ANTHROPIC_API_KEY": "sk-ant-secret"},
            _run=fake_run,
            _tag_exists=lambda *a, **k: False,
        )
    # Neither build nor push should have run.
    flat = [" ".join(c) for c in cmds]
    assert not any(c.startswith("docker build") for c in flat)
    assert not any(c.startswith("docker push") for c in flat)


def test_build_and_push_allows_clean_build_args(tmp_path):
    """Build args that are not platform secrets pass through without error."""
    repo = _write_repo(tmp_path)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)

        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    # A clean build arg (non-secret key, non-empty value) should not raise.
    result = _bci.build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        build_args={"PYTHON_VERSION": "3.11"},
        _run=fake_run,
        _tag_exists=lambda *a, **k: False,
        _scan_layers_fn=lambda *a, **k: [],
    )
    assert result.built is True
    flat = [" ".join(c) for c in cmds]
    assert any("--build-arg" in c and "PYTHON_VERSION=3.11" in c for c in flat)


def test_build_and_push_layer_credential_leak_fails_before_push(tmp_path):
    """A credential in built image layers fails the push AFTER build but BEFORE push."""
    repo = _write_repo(tmp_path)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)

        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    layer_scan_called_with = []

    def fake_layer_scan(image_ref, **kw):
        layer_scan_called_with.append(image_ref)
        return ["layer:some/layer.tar!root/.creds: contains github_app_token credential pattern"]

    with pytest.raises(_bci.CredentialLeakError, match="built image layers"):
        _bci.build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
            _run=fake_run,
            _tag_exists=lambda *a, **k: False,
            _scan_layers_fn=fake_layer_scan,
        )
    # Build was called, push was NOT. Match the push COMMAND specifically
    # (not a bare "push" substring, which false-matches the pytest tmp_path —
    # this test function's name contains "push", so the repo_dir arg baked into
    # the joined `docker build … <repo_dir>` string contains it too).
    flat = [" ".join(c) for c in cmds]
    assert any(c.startswith("docker build") for c in flat)
    assert not any(c.startswith("docker push") for c in flat)
    assert len(layer_scan_called_with) == 1


def test_build_and_push_config_env_baked_secret_blocked_before_push(tmp_path):
    """End-to-end: a platform secret baked into the built image's .config.Env is
    blocked by the REAL layer scanner before any push.

    A ``RUNNER_TOKEN`` inherited from a base image / injected via --build-arg lands
    in ``.config.Env`` without appearing in the source Dockerfile (which is clean
    here) or as a credential PATTERN in any layer file. This exercises the actual
    ``scan_image_layers_for_credentials`` config-env path through ``build_and_push``
    — not an injected fake — proving the "no platform creds baked into project
    images" guard is enforced, not merely a design note.
    """
    repo = _write_repo(tmp_path)  # clean Dockerfile (input scan passes)
    # docker-save archive: clean layer file, config.Env bakes a platform secret.
    archive = _classic_save_archive_with_env(
        ["PATH=/usr/bin", "RUNNER_TOKEN=baked-into-the-runtime-env"]
    )
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        # `docker save -o <path> <ref>` streams the archive to the -o destination;
        # the real scanner reads it back and finds the baked secret.
        if "save" in cmd and "-o" in cmd:
            with open(cmd[cmd.index("-o") + 1], "wb") as fh:
                fh.write(archive)

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        return P()

    with pytest.raises(_bci.CredentialLeakError, match="built image layers"):
        _bci.build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
            _run=fake_run,
            _tag_exists=lambda *a, **k: False,
            _assert_network_fn=lambda *a, **k: None,
        )
    flat = [" ".join(c) for c in cmds]
    assert any(c.startswith("docker build") for c in flat)
    assert not any(c.startswith("docker push") for c in flat)


# ---------------------------------------------------------------------------
# Build-network confinement: a repo-controlled Dockerfile's RUN steps run on the
# ORCHESTRATOR HOST. With the default docker build network they can reach
# 169.254.169.254 and trade the host's service-account identity for an access
# token. Every build must therefore run on a network from which the metadata
# server is provably unreachable — or not run at all.
# ---------------------------------------------------------------------------


def _ok_proc(stdout="", returncode=0, stderr=""):
    class P:
        pass

    p = P()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


def test_resolve_build_network_defaults_to_none(monkeypatch):
    """No operator opt-in => RUN steps get NO network namespace at all."""
    monkeypatch.delenv("COMPUTE_IMAGE_BUILD_NETWORK", raising=False)
    assert _bci.resolve_build_network() == "none"
    assert _bci.resolve_build_network("") == "none"
    # Operator opt-in is honored (and is then probed, see below).
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_NETWORK", "bp-image-build")
    assert _bci.resolve_build_network() == "bp-image-build"
    # An explicit argument still wins (CLI / tests).
    assert _bci.resolve_build_network("none") == "none"


def test_build_and_push_confines_the_run_step_network(tmp_path):
    """`docker build` is invoked with an explicit, confined --network.

    Omitting --network is what selected the default bridge, from which the GCP
    metadata server answers. The flag is now non-optional.
    """
    repo = _write_repo(tmp_path)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        return _ok_proc(stdout="reg/repo/img@sha256:" + "b" * 64)

    _bci.build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        _run=fake_run,
        _tag_exists=lambda *a, **k: False,
        _scan_layers_fn=lambda *a, **k: [],
        build_network="none",
    )
    build_cmd = next(c for c in cmds if c[:2] == ["docker", "build"])
    assert "--network=none" in build_cmd, (
        "docker build ran without an explicit confined --network — its RUN steps "
        "would reach 169.254.169.254 from the orchestrator host"
    )


@pytest.mark.parametrize("net", ["host", "bridge", "default", "", "HOST"])
def test_build_and_push_refuses_unconfined_networks(tmp_path, net):
    """host/bridge/default all reach the metadata server — the build is refused."""
    repo = _write_repo(tmp_path)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        return _ok_proc()

    with pytest.raises(_bci.BuildNetworkError):
        _bci.build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
            _run=fake_run,
            _tag_exists=lambda *a, **k: False,
            _scan_layers_fn=lambda *a, **k: [],
            build_network=net,
        )
    # Nothing was built: the refusal happens BEFORE any RUN step executes.
    assert not any(c[:2] == ["docker", "build"] for c in cmds)


def test_build_refused_when_the_build_network_reaches_the_metadata_server(tmp_path):
    """A named network is PROBED; if metadata answers, the build never starts."""
    repo = _write_repo(tmp_path)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        if cmd[:2] == ["docker", "run"]:
            # The probe container reached 169.254.169.254.
            return _ok_proc(stdout="BP_METADATA_REACHABLE\n")
        return _ok_proc(stdout="reg/repo/img@sha256:" + "b" * 64)

    with pytest.raises(_bci.BuildNetworkError, match="metadata"):
        _bci.build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
            _run=fake_run,
            _tag_exists=lambda *a, **k: False,
            _scan_layers_fn=lambda *a, **k: [],
            build_network="leaky-net",
        )
    assert any(c[:2] == ["docker", "run"] for c in cmds), "the network was not probed"
    assert not any(c[:2] == ["docker", "build"] for c in cmds), (
        "docker build ran on a network from which the metadata server is reachable"
    )


def test_build_proceeds_on_a_network_that_blocks_the_metadata_server(tmp_path):
    """An operator network whose probe reports BLOCKED is usable for RUN steps."""
    repo = _write_repo(tmp_path)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        if cmd[:2] == ["docker", "run"]:
            return _ok_proc(stdout="BP_METADATA_BLOCKED\n")
        return _ok_proc(stdout="reg/repo/img@sha256:" + "c" * 64)

    result = _bci.build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        _run=fake_run,
        _tag_exists=lambda *a, **k: False,
        _scan_layers_fn=lambda *a, **k: [],
        build_network="walled-net",
    )
    assert result.built is True
    build_cmd = next(c for c in cmds if c[:2] == ["docker", "build"])
    assert "--network=walled-net" in build_cmd


def test_metadata_probe_fails_closed_when_it_cannot_run(tmp_path):
    """A probe that never ran proves NOTHING — the build is refused, not waved through.

    Without the sentinel positive-control, a probe that failed for an unrelated
    reason (image pull failed, docker down) is indistinguishable from
    "metadata unreachable", which would silently re-open the hole.
    """
    repo = _write_repo(tmp_path)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        if cmd[:2] == ["docker", "run"]:
            # Probe image could not be pulled: non-zero exit, no sentinel.
            return _ok_proc(returncode=125, stdout="", stderr="Unable to find image")
        return _ok_proc(stdout="reg/repo/img@sha256:" + "d" * 64)

    with pytest.raises(_bci.BuildNetworkError, match="failing closed"):
        _bci.build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
            _run=fake_run,
            _tag_exists=lambda *a, **k: False,
            _scan_layers_fn=lambda *a, **k: [],
            build_network="unknown-net",
        )
    assert not any(c[:2] == ["docker", "build"] for c in cmds)


def test_metadata_probe_fails_closed_on_a_silent_zero_exit(tmp_path):
    """Exit 0 with no sentinel (e.g. an entrypoint that ignored the script) is refused."""

    def fake_run(cmd, **kw):
        if cmd[:2] == ["docker", "run"]:
            return _ok_proc(returncode=0, stdout="")
        return _ok_proc()

    with pytest.raises(_bci.BuildNetworkError, match="failing closed"):
        _bci.assert_build_network_confined("quiet-net", _run=fake_run)


def test_metadata_probe_covers_both_metadata_endpoints():
    """The probe script tries the raw link-local IP and the DNS alias."""
    assert "169.254.169.254" in _bci._METADATA_PROBE_SCRIPT
    assert "metadata.google.internal" in _bci._METADATA_PROBE_SCRIPT


# ---------------------------------------------------------------------------
# Digest resolution on the already-present fast path (unchanged-deps re-trigger).
#
# The fast path used to read the digest from the LOCAL docker store only, so on a
# fresh/pruned host it resolved None, returned a tag-only pinned_ref, and the
# caller persisted digest=NULL — silently destroying the project's digest pin.
# ---------------------------------------------------------------------------


def test_already_present_resolves_digest_from_registry_with_an_empty_local_store(tmp_path):
    """Fresh host (no local image): the digest still resolves — from the REGISTRY."""
    repo = _write_repo(tmp_path)
    registry_digest = "sha256:" + "e" * 64
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        # The digest is read daemon-free from the REGISTRY via
        # `gcloud artifacts docker images describe --format=json` (#15569) —
        # it never consults the local docker store, so a fresh/pruned host
        # still resolves the pin.
        if cmd[:5] == ["gcloud", "artifacts", "docker", "images", "describe"]:
            return _ok_proc(
                stdout=json.dumps({"image_summary": {"digest": registry_digest}})
            )
        if cmd[:3] == ["docker", "image", "inspect"]:
            # EMPTY LOCAL STORE — this host never built or pulled the image.
            return _ok_proc(returncode=1, stdout="", stderr="No such image")
        return _ok_proc()

    result = _bci.build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        _run=fake_run,
        _tag_exists=lambda *a, **k: True,  # unchanged deps: already in the registry
    )
    assert result.already_present is True
    assert result.built is False
    assert result.digest == registry_digest, (
        "the already-present fast path resolved no digest from the registry — on a "
        "fresh host it would return a tag-only ref and NULL the project's stored pin"
    )
    assert result.pinned_ref == "reg/repo/img@" + registry_digest
    assert result.digest_unknown is False
    assert any(
        c[:5] == ["gcloud", "artifacts", "docker", "images", "describe"] for c in cmds
    )


def test_already_present_marks_the_digest_unknown_when_no_source_resolves_it(tmp_path):
    """Registry AND local store both silent => digest is UNKNOWN, and says so.

    ``digest=None`` here does NOT mean "this image has no digest"; it means we
    could not read it. Persisters key on ``digest_unknown`` to refuse to overwrite
    a stored pin with NULL.
    """
    repo = _write_repo(tmp_path)

    def fake_run(cmd, **kw):
        return _ok_proc(returncode=1, stdout="", stderr="boom")

    result = _bci.build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        _run=fake_run,
        _tag_exists=lambda *a, **k: True,
    )
    assert result.digest is None
    assert result.digest_unknown is True
    assert result.pinned_ref == "reg/repo/img:" + result.dep_hash


def test_resolve_registry_digest_handles_a_multi_arch_manifest_list():
    """``gcloud artifacts docker images describe`` returns image_summary.digest."""
    digest = "sha256:" + "f" * 64

    def fake_run(cmd, **kw):
        assert "gcloud" in cmd and "describe" in cmd, f"Expected gcloud describe, got {cmd}"
        return _ok_proc(stdout=json.dumps({"image_summary": {"digest": digest}}))

    assert _bci.resolve_registry_digest("reg/repo/img", "t", _run=fake_run) == digest


def test_resolve_registry_digest_returns_none_on_garbage():
    """A non-JSON / error / missing-key response resolves to None rather than a bogus digest."""

    def fake_run(cmd, **kw):
        return _ok_proc(stdout="not json at all")

    assert _bci.resolve_registry_digest("reg/repo/img", "t", _run=fake_run) is None

    def failing_run(cmd, **kw):
        raise OSError("gcloud not installed")

    assert _bci.resolve_registry_digest("reg/repo/img", "t", _run=failing_run) is None

    def missing_key_run(cmd, **kw):
        return _ok_proc(stdout=json.dumps({"image_summary": {}}))

    assert _bci.resolve_registry_digest("reg/repo/img", "t", _run=missing_key_run) is None


def test_registry_tag_exists_uses_gcloud_describe():
    """registry_tag_exists shells to gcloud (not docker), fail-open on errors."""
    commands_seen = []

    def capture(cmd, **kw):
        commands_seen.append(cmd)
        return _ok_proc()

    result = _bci.registry_tag_exists("reg/repo/img", "mytag", _run=capture)
    assert result is True
    assert any("gcloud" in c and "describe" in c for c in commands_seen), commands_seen
    assert any("reg/repo/img:mytag" in " ".join(c) for c in commands_seen), commands_seen

    # Non-zero exit → absent (not an exception)
    def not_found(cmd, **kw):
        class _Proc:
            returncode = 1
            stdout = stderr = ""
        return _Proc()

    assert _bci.registry_tag_exists("reg/repo/img", "missing", _run=not_found) is False

    # OSError → absent (fail-open)
    def broken(cmd, **kw):
        raise OSError("no gcloud")

    assert _bci.registry_tag_exists("reg/repo/img", "missing", _run=broken) is False


def test_resolve_registry_digest_uses_gcloud_describe():
    """resolve_registry_digest shells to gcloud and parses image_summary.digest."""
    digest = "sha256:" + "a" * 64
    commands_seen = []

    def capture(cmd, **kw):
        commands_seen.append(cmd)
        return _ok_proc(stdout=json.dumps({"image_summary": {"digest": digest}}))

    result = _bci.resolve_registry_digest("reg/repo/img", "v1", _run=capture)
    assert result == digest
    assert any("gcloud" in c and "describe" in c for c in commands_seen), commands_seen

    # Non-zero exit → None
    def not_found(cmd, **kw):
        class _Proc:
            returncode = 1
            stdout = stderr = ""
        return _Proc()

    assert _bci.resolve_registry_digest("reg/repo/img", "v1", _run=not_found) is None

    # OSError → None (fail-open)
    def broken(cmd, **kw):
        raise OSError("no gcloud")

    assert _bci.resolve_registry_digest("reg/repo/img", "v1", _run=broken) is None


# ---------------------------------------------------------------------------
# BuildControl: the outer deadline must actually STOP the build, not abandon it.
# ---------------------------------------------------------------------------


def test_supervised_run_without_a_control_is_plain_subprocess_run():
    """No control published => unchanged behavior (CLI, reaper, unit tests)."""
    assert _bci.current_build_control() is None
    proc = _bci.supervised_run(
        [sys.executable, "-c", "print('hello')"], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0
    assert "hello" in proc.stdout


def test_abort_kills_a_running_child_and_raises():
    """abort() SIGKILLs the in-flight docker child; the blocking call returns at once."""
    control = _bci.BuildControl()
    token = _bci.set_build_control(control)
    try:
        threading.Timer(0.5, control.abort).start()
        started = time.monotonic()
        with pytest.raises(_bci.BuildAborted):
            _bci.supervised_run(
                # Stand-in for `docker build`: a long-running child.
                [sys.executable, "-c", "import time; time.sleep(120)"],
                capture_output=True,
                text=True,
                timeout=180,
            )
        elapsed = time.monotonic() - started
    finally:
        _bci.reset_build_control(token)
    assert elapsed < 20, (
        f"the abort did not stop the child (took {elapsed:.1f}s) — a deadline that "
        "cannot stop the build leaves it running on the orchestrator host"
    )


def test_an_aborted_control_refuses_to_start_new_children():
    """Abort is sticky: a late `docker push` cannot slip out after the deadline."""
    control = _bci.BuildControl()
    control.abort()
    token = _bci.set_build_control(control)
    try:
        with pytest.raises(_bci.BuildAborted):
            _bci.supervised_run(
                [sys.executable, "-c", "print('should never run')"],
                capture_output=True,
                text=True,
                timeout=30,
            )
    finally:
        _bci.reset_build_control(token)


@pytest.mark.asyncio
async def test_build_control_reaches_the_worker_thread():
    """The ContextVar transport the router relies on: control survives to_thread.

    ``asyncio.to_thread`` copies the calling context into the worker, so a control
    published by the request handler before ``create_task`` is visible to the
    blocking build code — that is what lets the deadline kill the docker children.
    """
    control = _bci.BuildControl()
    token = _bci.set_build_control(control)
    try:
        task = asyncio.create_task(asyncio.to_thread(_bci.current_build_control))
    finally:
        _bci.reset_build_control(token)
    assert await task is control


# ---------------------------------------------------------------------------
# scan_image_layers_for_credentials: classic + OCI archive layouts, fail-closed
# ---------------------------------------------------------------------------

# A credential blob that trips the github_app_token pattern (ghs_ + >=20 chars).
_LEAKED_CRED = b"export TOKEN=ghs_" + b"A" * 24 + b"\n"


def _tar_bytes(entries: dict) -> bytes:
    """Build an uncompressed tar archive from {member_name: bytes}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _fake_docker_save(stdout_bytes: bytes):
    """Return a _run callable that emulates a successful `docker save`.

    The real command is now ``docker save -o <path> <ref>`` — the archive is
    streamed to a FILE rather than piped back through the parent's memory (a
    multi-GB CUDA image OOM'd the API process when it was buffered). The fake
    therefore writes the archive to the ``-o`` destination.

    It ALSO still returns the bytes on ``stdout``, which the streaming
    implementation never reads. That is deliberate: it keeps this fake honest for
    the memory test below, which asserts the scanner does not materialize the
    archive even when a pipe-shaped payload is right there for the taking.
    """

    def _run(cmd, **kw):
        if "-o" in cmd:
            out_path = cmd[cmd.index("-o") + 1]
            with open(out_path, "wb") as fh:
                fh.write(stdout_bytes)

        class P:
            returncode = 0
            stdout = stdout_bytes
            stderr = b""

        return P()

    return _run


def _classic_save_archive(leak: bytes) -> bytes:
    """A classic docker-save archive: <hash>/layer.tar members + manifest.json."""
    layer_tar = _tar_bytes({"root/.creds": leak})
    return _tar_bytes(
        {
            "0a1b2c/layer.tar": layer_tar,
            "config.json": b"{}",
            "manifest.json": b'[{"Layers":["0a1b2c/layer.tar"]}]',
        }
    )


def _oci_save_archive(leak: bytes) -> bytes:
    """An OCI-layout docker-save archive: index.json + blobs/sha256/<digest>."""
    layer_tar = _tar_bytes({"root/.creds": leak})
    layer_digest = "l" * 64
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": "sha256:" + ("c" * 64)},
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                    "digest": "sha256:" + layer_digest,
                }
            ],
        }
    ).encode()
    manifest_digest = "m" * 64
    index = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": "sha256:" + manifest_digest,
                }
            ],
        }
    ).encode()
    return _tar_bytes(
        {
            "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
            "index.json": index,
            "blobs/sha256/" + manifest_digest: manifest,
            "blobs/sha256/" + layer_digest: layer_tar,
            "blobs/sha256/" + ("c" * 64): b"{}",
        }
    )


def test_scan_image_layers_classic_format_detects_leak():
    """Classic docker-save layer.tar members are still scanned (regression)."""
    findings = _bci.scan_image_layers_for_credentials(
        "img:tag", _run=_fake_docker_save(_classic_save_archive(_LEAKED_CRED))
    )
    assert any("github_app_token" in f for f in findings), findings


def test_scan_image_layers_classic_format_clean_passes():
    """A classic archive with no credential material yields no findings."""
    findings = _bci.scan_image_layers_for_credentials(
        "img:tag", _run=_fake_docker_save(_classic_save_archive(b"nothing secret here\n"))
    )
    assert findings == []


def test_scan_image_layers_oci_format_detects_leak():
    """OCI-layout images (blobs/sha256/...) are resolved via index/manifest and scanned."""
    findings = _bci.scan_image_layers_for_credentials(
        "img:tag", _run=_fake_docker_save(_oci_save_archive(_LEAKED_CRED))
    )
    assert any("github_app_token" in f for f in findings), findings


def test_scan_image_layers_oci_format_clean_passes():
    """A clean OCI-layout archive resolves its layer and finds nothing."""
    findings = _bci.scan_image_layers_for_credentials(
        "img:tag", _run=_fake_docker_save(_oci_save_archive(b"nothing secret here\n"))
    )
    assert findings == []


# ---------------------------------------------------------------------------
# scan_image_layers_for_credentials ALSO scans the image config .Env: a platform
# secret baked into the runtime environment (base image, --build-arg, RUN export)
# is invisible to both the layer-file pattern scan and the source-Dockerfile scan.
# ---------------------------------------------------------------------------

_CLEAN_LAYER_FILE = b"nothing secret here\n"


def _classic_save_archive_with_env(env_entries: list) -> bytes:
    """Classic docker-save archive whose manifest points at a config with .Env."""
    layer_tar = _tar_bytes({"root/app": _CLEAN_LAYER_FILE})
    config = json.dumps({"config": {"Env": env_entries}}).encode()
    return _tar_bytes(
        {
            "0a1b2c/layer.tar": layer_tar,
            "config.json": config,
            "manifest.json": b'[{"Config":"config.json","Layers":["0a1b2c/layer.tar"]}]',
        }
    )


def _oci_save_archive_with_env(env_entries: list) -> bytes:
    """OCI-layout docker-save archive whose config blob carries .config.Env."""
    layer_tar = _tar_bytes({"root/app": _CLEAN_LAYER_FILE})
    layer_digest = "l" * 64
    config_digest = "c" * 64
    config = json.dumps({"config": {"Env": env_entries}}).encode()
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": "sha256:" + config_digest},
            "layers": [{"digest": "sha256:" + layer_digest}],
        }
    ).encode()
    manifest_digest = "m" * 64
    index = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [{"digest": "sha256:" + manifest_digest}],
        }
    ).encode()
    return _tar_bytes(
        {
            "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
            "index.json": index,
            "blobs/sha256/" + manifest_digest: manifest,
            "blobs/sha256/" + layer_digest: layer_tar,
            "blobs/sha256/" + config_digest: config,
        }
    )


def test_scan_config_env_pure_flags_secret_name_with_value():
    """The pure config-Env scanner flags a secret-named var carrying a value."""
    findings = _bci._scan_config_env_for_secrets(
        "config:x", ["PATH=/usr/bin", "RUNNER_TOKEN=deadbeef-uuid-not-a-regex-match"]
    )
    assert any("RUNNER_TOKEN" in f for f in findings), findings
    assert all("PATH" not in f for f in findings)


def test_scan_config_env_pure_ignores_empty_and_benign():
    """Empty secret values and benign names produce no findings."""
    findings = _bci._scan_config_env_for_secrets(
        "config:x", ["ANTHROPIC_API_KEY=", "LANG=C.UTF-8", "NODE_ENV=production"]
    )
    assert findings == []


def test_scan_image_layers_classic_config_env_secret_detected():
    """A platform secret baked into the classic-layout image config .Env is caught.

    The value (a bare UUID-ish orchestrator runner token) matches NO credential
    regex and appears in no layer file — only the config Env name-scan catches it.
    """
    archive = _classic_save_archive_with_env(
        ["PATH=/usr/local/bin", "BLUEPRINT_RUNNER_TOKEN=11111111-2222-3333"]
    )
    findings = _bci.scan_image_layers_for_credentials(
        "img:tag", _run=_fake_docker_save(archive)
    )
    assert any(
        "BLUEPRINT_RUNNER_TOKEN" in f and "config Env" in f for f in findings
    ), findings


def test_scan_image_layers_classic_config_env_clean_passes():
    """A classic image whose config Env holds only benign vars yields no findings."""
    archive = _classic_save_archive_with_env(
        ["PATH=/usr/local/bin", "LANG=C.UTF-8", "PYTHONUNBUFFERED=1"]
    )
    findings = _bci.scan_image_layers_for_credentials(
        "img:tag", _run=_fake_docker_save(archive)
    )
    assert findings == []


def test_scan_image_layers_oci_config_env_secret_detected():
    """A platform secret baked into an OCI-layout image config .Env is caught."""
    archive = _oci_save_archive_with_env(
        ["PATH=/usr/bin", "BLUEPRINT_ORCHESTRATOR_URL=https://orch.internal"]
    )
    findings = _bci.scan_image_layers_for_credentials(
        "img:tag", _run=_fake_docker_save(archive)
    )
    assert any(
        "BLUEPRINT_ORCHESTRATOR_URL" in f and "config Env" in f for f in findings
    ), findings


def test_scan_image_layers_oci_config_env_clean_passes():
    """A clean OCI-layout config Env yields no findings."""
    archive = _oci_save_archive_with_env(["PATH=/usr/bin", "TERM=xterm"])
    findings = _bci.scan_image_layers_for_credentials(
        "img:tag", _run=_fake_docker_save(archive)
    )
    assert findings == []


def test_scan_image_layers_no_recognizable_layers_fails_closed():
    """An archive with neither layer.tar members nor an OCI store must ERROR, not pass."""
    # Only a manifest.json — no layer.tar, no index.json/blobs.
    bogus = _tar_bytes({"manifest.json": b"[]", "config.json": b"{}"})
    with pytest.raises(RuntimeError, match="no recognizable layers"):
        _bci.scan_image_layers_for_credentials(
            "img:tag", _run=_fake_docker_save(bogus)
        )


def test_scan_image_layers_oci_unresolvable_blobs_fails_closed():
    """An OCI index whose referenced blobs are absent yields zero layers → fail closed."""
    index = json.dumps(
        {"schemaVersion": 2, "manifests": [{"digest": "sha256:" + ("m" * 64)}]}
    ).encode()
    # index.json present but the manifest blob it points at is missing.
    bogus = _tar_bytes({"oci-layout": b"{}", "index.json": index})
    with pytest.raises(RuntimeError, match="no recognizable layers"):
        _bci.scan_image_layers_for_credentials(
            "img:tag", _run=_fake_docker_save(bogus)
        )


def test_scan_image_layers_streams_to_disk_never_buffers_the_archive():
    """`docker save` must write to a FILE, not be drained into memory.

    The credential layer-scan runs on real compute images — a CUDA base plus a
    torch wheel is comfortably multi-GB. Buffering the `docker save` archive
    (`capture_output=True` → `io.BytesIO(proc.stdout)` → per-layer `bytes`) held
    the whole image in RAM two or three times over and OOM'd the API process that
    a project MEMBER could trigger over HTTP.
    """
    seen_cmds = []

    def _run(cmd, **kw):
        seen_cmds.append(cmd)
        out_path = cmd[cmd.index("-o") + 1]
        with open(out_path, "wb") as fh:
            fh.write(_classic_save_archive(b"clean\n"))

        class P:
            returncode = 0
            stdout = b""  # nothing is piped back
            stderr = b""

        return P()

    findings = _bci.scan_image_layers_for_credentials("img:tag", _run=_run)

    assert findings == []
    cmd = seen_cmds[0]
    assert "-o" in cmd, f"docker save must stream to a file, got: {cmd}"
    # The archive path is a real temp file the scanner owns, not a pipe.
    assert cmd[cmd.index("-o") + 1].endswith(".tar")
    assert cmd[-1] == "img:tag"


def test_scan_image_layers_peak_memory_is_bounded_by_file_cap_not_image_size(tmp_path):
    """Peak memory during the scan must NOT scale with the image size.

    This is the OOM, as a measurement. The archive holds a 48 MiB blob (standing
    in for the CUDA/torch payload that makes a real compute image multi-GB) plus a
    small file carrying a leaked credential.

    The fake `docker save` models the two transports HONESTLY, which is what makes
    the assertion mean something:

    * ``-o <path>`` (what the streaming scanner asks for) — the archive is copied
      to the destination on disk, never through Python memory, and stdout is
      empty.
    * no ``-o`` (what a `capture_output=True` implementation asks for) — the
      archive has to be pulled into a bytes object to be handed back on stdout,
      exactly as a real pipe would. That allocation lands inside the measurement
      window, so a buffering implementation is caught paying for it.

    A streaming scan seeks past the oversized member without ever reading it and
    stays near the 10 MiB per-file cap; a buffering one holds the image.
    """
    big_blob = b"\0" * (48 * 1024 * 1024)  # skipped: over the 10 MiB per-file cap
    layer_tar = _tar_bytes({"opt/cuda/payload.bin": big_blob, "root/.creds": _LEAKED_CRED})
    archive = _tar_bytes(
        {
            "0a1b2c/layer.tar": layer_tar,
            "manifest.json": b'[{"Layers":["0a1b2c/layer.tar"]}]',
        }
    )
    archive_size = len(archive)
    assert archive_size > 48 * 1024 * 1024

    # Stage the archive on disk, then drop every in-memory reference to it, so
    # ONLY the scanner's own allocations land in the tracemalloc window.
    src = tmp_path / "image-src.tar"
    src.write_bytes(archive)
    del big_blob, layer_tar, archive

    def _run(cmd, **kw):
        if "-o" in cmd:
            # Streams to disk — no Python-level buffer of the archive.
            shutil.copyfile(src, cmd[cmd.index("-o") + 1])
            stdout = b""
        else:
            # A real pipe: the parent must materialize the whole archive.
            stdout = src.read_bytes()

        class P:
            returncode = 0
            stderr = b""

        P.stdout = stdout
        return P()

    tracemalloc.start()
    try:
        findings = _bci.scan_image_layers_for_credentials("img:tag", _run=_run)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # The scan still WORKS: the small credential file inside the huge layer is found.
    assert any("github_app_token" in f for f in findings), findings

    # And it did not pay for the image's size in RAM. The ceiling is generous
    # (well under half the archive) so this asserts the SHAPE — bounded, not
    # proportional — rather than a brittle exact number.
    ceiling = 20 * 1024 * 1024
    assert peak < ceiling, (
        f"layer-scan peaked at {peak / 1024 / 1024:.1f} MiB on a "
        f"{archive_size / 1024 / 1024:.1f} MiB archive — it is buffering the "
        "image instead of streaming it (this is the OOM)"
    )


def test_build_and_push_missing_dockerfile_raises(tmp_path):
    repo = str(tmp_path / "empty")
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError):
        _bci.build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=[],
            _tag_exists=lambda *a, **k: False,
        )


def test_build_and_push_dry_run_does_not_build(tmp_path):
    repo = _write_repo(tmp_path)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)

        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    result = _bci.build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        dry_run=True,
        _run=fake_run,
        _tag_exists=lambda *a, **k: False,
    )
    assert result.built is False
    assert result.already_present is False
    assert not any("build" in c or "push" in c for c in cmds)


# ---------------------------------------------------------------------------
# Backend orchestration (compute_backends.build_project_compute_image)
# ---------------------------------------------------------------------------


def test_resolve_project_compute_image_build_extracts_config():
    from app import compute_backends as cb

    manifest_compute = {
        "image": {
            "name": "reg/repo/img:tag",
            "build": {
                "dockerfile": ".blueprint/Dockerfile.compute",
                "parent": "python:3.11-slim",
                "build_args": {"A": "1"},
            },
        }
    }
    cfg = cb.resolve_project_compute_image_build(manifest_compute)
    assert cfg is not None
    assert cfg["name"] == "reg/repo/img:tag"
    assert cfg["parent"] == "python:3.11-slim"
    assert cfg["build_args"] == {"A": "1"}


def test_resolve_project_compute_image_build_none_without_build():
    from app import compute_backends as cb

    assert cb.resolve_project_compute_image_build({"image": {"name": "reg/img:t", "build": None}}) is None
    assert cb.resolve_project_compute_image_build({"image": None}) is None
    assert cb.resolve_project_compute_image_build(None) is None


@pytest.mark.asyncio
async def test_build_project_compute_image_orchestrates_clone_and_build():
    from app import compute_backends as cb
    from app.models import Project

    project = Project(
        name="p",
        github_owner="octo",
        github_repo="proj",
    )

    clone_seen = {}

    def fake_clone(owner, repo, ref, dest):
        clone_seen["owner"] = owner
        clone_seen["repo"] = repo
        clone_seen["ref"] = ref
        # Materialize a minimal repo so _discover_dep_files finds inputs.
        os.makedirs(os.path.join(dest, ".blueprint"), exist_ok=True)
        with open(os.path.join(dest, ".blueprint", "Dockerfile.compute"), "wb") as fh:
            fh.write(b"FROM python:3.11-slim\n")
        with open(os.path.join(dest, "requirements.txt"), "wb") as fh:
            fh.write(b"numpy==1.26\n")

    captured = {}

    def fake_build(**kwargs):
        captured.update(kwargs)
        return _bci.BuildResult(
            pinned_ref="reg/repo/proj@sha256:" + "c" * 64,
            tag="deadbeef",
            dep_hash="deadbeef",
            already_present=False,
            built=True,
        )

    result = await cb.build_project_compute_image(
        project,
        registry_repo="reg/repo",
        build_config={
            "name": "reg/repo/proj:latest",
            "dockerfile": ".blueprint/Dockerfile.compute",
            "parent": "python:3.11-slim",
            "build_args": {},
        },
        ref="main",
        _clone_fn=fake_clone,
        _build_fn=fake_build,
    )
    assert clone_seen["owner"] == "octo"
    assert clone_seen["ref"] == "main"
    assert result.pinned_ref == "reg/repo/proj@sha256:" + "c" * 64
    # Discovered requirements.txt was passed as a dependency input.
    assert "requirements.txt" in captured["dep_files"]
    assert captured["image_name"] == "proj"


@pytest.mark.asyncio
async def test_build_project_compute_image_requires_repo():
    from app import compute_backends as cb
    from app.models import Project

    project = Project(name="p", github_owner="", github_repo="")
    with pytest.raises(ValueError):
        await cb.build_project_compute_image(
            project,
            registry_repo="reg/repo",
            build_config={"name": "reg/repo/proj:latest"},
            _clone_fn=lambda *a: None,
            _build_fn=lambda **k: None,
        )


# ---------------------------------------------------------------------------
# ProjectComputeImage persistence (record_project_compute_image_build).
#
# The baking pipeline returns a BuildResult (pinned_ref/tag/dep_hash/built) but
# nothing durably recorded it. These cover the additive UPSERT helper: a fresh
# insert, an in-place UPSERT keyed on (project_id, dep_hash), and digest parsing
# from a @sha256 pinned_ref vs a tag-only ref.
# ---------------------------------------------------------------------------
def _build_result(*, pinned_ref, tag="latest", dep_hash="deadbeef", built=True):
    from app.compute_image_build import BuildResult

    return BuildResult(
        pinned_ref=pinned_ref,
        tag=tag,
        dep_hash=dep_hash,
        already_present=not built,
        built=built,
    )


async def _make_project(db_session, name="Compute Image Proj"):
    from app.models import Project

    project = Project(name=name, user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


# ---------------------------------------------------------------------------
# resolve_digest_pinned_default_image: pin a mutable manifest tag to a durable
# content digest when a ProjectComputeImage build record exists (design gap G3).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_resolve_digest_pin_returns_none_for_empty_input(db_session):
    from app import compute_backends as cb
    import uuid as _uuid

    assert await cb.resolve_digest_pinned_default_image(db_session, _uuid.uuid4(), None) is None
    assert await cb.resolve_digest_pinned_default_image(db_session, _uuid.uuid4(), "") is None


@pytest.mark.asyncio
async def test_resolve_digest_pin_passthrough_when_already_pinned(db_session):
    from app import compute_backends as cb
    import uuid as _uuid

    already = "ghcr.io/org/base@sha256:" + "a" * 64
    # Already content-addressed: returned unchanged without any DB lookup.
    assert (
        await cb.resolve_digest_pinned_default_image(db_session, _uuid.uuid4(), already)
        == already
    )


@pytest.mark.asyncio
async def test_resolve_digest_pin_falls_back_to_tag_when_no_row(db_session):
    from app import compute_backends as cb

    project = await _make_project(db_session)
    tag = "ghcr.io/org/base:latest"
    # No build record for this project -> fail-open to the mutable tag.
    assert (
        await cb.resolve_digest_pinned_default_image(db_session, project.id, tag) == tag
    )


@pytest.mark.asyncio
async def test_resolve_digest_pin_uses_built_digest_row(db_session):
    from app import compute_backends as cb
    from app.models import ProjectComputeImage

    project = await _make_project(db_session)
    pinned = "ghcr.io/org/base@sha256:" + "b" * 64
    db_session.add(
        ProjectComputeImage(
            project_id=project.id,
            image_name="base",
            registry_repo="ghcr.io/org",
            dep_hash="h1",
            tag="latest",
            pinned_ref=pinned,
            digest="sha256:" + "b" * 64,
            built=True,
        )
    )
    await db_session.commit()

    assert (
        await cb.resolve_digest_pinned_default_image(
            db_session, project.id, "ghcr.io/org/base:latest"
        )
        == pinned
    )


@pytest.mark.asyncio
async def test_resolve_digest_pin_ignores_unbuilt_and_digestless_rows(db_session):
    from app import compute_backends as cb
    from app.models import ProjectComputeImage

    project = await _make_project(db_session)
    tag = "ghcr.io/org/base:latest"
    # A row that never finished building (built=False) and one with no digest
    # must both be ignored; the resolver falls back to the mutable tag.
    db_session.add(
        ProjectComputeImage(
            project_id=project.id,
            image_name="base",
            registry_repo="ghcr.io/org",
            dep_hash="unbuilt",
            tag="latest",
            pinned_ref="ghcr.io/org/base@sha256:" + "c" * 64,
            digest="sha256:" + "c" * 64,
            built=False,
        )
    )
    db_session.add(
        ProjectComputeImage(
            project_id=project.id,
            image_name="base",
            registry_repo="ghcr.io/org",
            dep_hash="digestless",
            tag="latest",
            pinned_ref="ghcr.io/org/base:latest",
            digest=None,
            built=True,
        )
    )
    await db_session.commit()

    assert (
        await cb.resolve_digest_pinned_default_image(db_session, project.id, tag) == tag
    )


# ---------------------------------------------------------------------------
# is_digest_pinned + operator-visible mutable-tag signal: the images-first
# substrate promises run-to-run reproducibility via digest pinning, so a
# fail-open to a mutable tag must be visible, not a silent debug note.
# ---------------------------------------------------------------------------
def test_is_digest_pinned_truth_table():
    from app import compute_backends as cb

    assert cb.is_digest_pinned("ghcr.io/org/base@sha256:" + "a" * 64) is True
    # A plain mutable tag is NOT pinned.
    assert cb.is_digest_pinned("ghcr.io/org/base:latest") is False
    assert cb.is_digest_pinned("ghcr.io/org/base") is False
    # Empty / absent references are not pinned.
    assert cb.is_digest_pinned(None) is False
    assert cb.is_digest_pinned("") is False


@pytest.mark.asyncio
async def test_resolve_digest_pin_warns_when_falling_open_to_mutable_tag(
    db_session, caplog
):
    """No build record -> fail-open to the mutable tag AND emit an operator-visible
    warning (not a silent debug note) that reproducibility is not guaranteed."""
    import logging as _logging

    from app import compute_backends as cb

    project = await _make_project(db_session)
    tag = "ghcr.io/org/base:latest"
    with caplog.at_level(_logging.WARNING, logger="app.compute_backends"):
        result = await cb.resolve_digest_pinned_default_image(
            db_session, project.id, tag
        )
    assert result == tag
    warnings = [
        r for r in caplog.records
        if r.levelno == _logging.WARNING and "MUTABLE tag" in r.getMessage()
    ]
    assert warnings, "expected an operator-visible mutable-tag warning"
    assert "reproducibility is NOT guaranteed" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_resolve_digest_pin_does_not_warn_when_pinned(db_session, caplog):
    """A build record with a digest resolves to the pinned ref with no warning."""
    import logging as _logging

    from app import compute_backends as cb
    from app.models import ProjectComputeImage

    project = await _make_project(db_session)
    pinned = "ghcr.io/org/base@sha256:" + "d" * 64
    db_session.add(
        ProjectComputeImage(
            project_id=project.id,
            image_name="base",
            registry_repo="ghcr.io/org",
            dep_hash="h-pinned",
            tag="latest",
            pinned_ref=pinned,
            digest="sha256:" + "d" * 64,
            built=True,
        )
    )
    await db_session.commit()
    with caplog.at_level(_logging.WARNING, logger="app.compute_backends"):
        result = await cb.resolve_digest_pinned_default_image(
            db_session, project.id, "ghcr.io/org/base:latest"
        )
    assert result == pinned
    assert not [
        r for r in caplog.records
        if r.levelno == _logging.WARNING and "MUTABLE tag" in r.getMessage()
    ]


# ---------------------------------------------------------------------------
# _load_baked_image_ref: surface project-baked compute image ref for agent context.
#
# The strategic spawning pipeline merges baked_image_ref into local_runner_status
# so authoring agents can prefer the project image over a raw NGC URI.  These
# tests cover the query helper directly:
#   - no rows → None
#   - built=True row with pinned_ref → returns pinned_ref
#   - built=False row ignored
#   - returns most recent when multiple rows exist
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dispatch_defaults_project_image(db_session):
    """_load_baked_image_ref returns the most recent built digest-pinned ref,
    so the strategic spawner can merge it into local_runner_status and agents
    default compute_experiments to the project-baked image."""
    from app.models import ProjectComputeImage
    from app.strategic_local_runner import _load_baked_image_ref

    project = await _make_project(db_session, name="Baked Image Context Proj")

    # No rows → None (project has not been built yet).
    assert await _load_baked_image_ref(db_session, project.id) is None

    pinned = "ghcr.io/org/compute-proj@sha256:" + "e" * 64
    db_session.add(
        ProjectComputeImage(
            project_id=project.id,
            image_name="compute-proj",
            registry_repo="ghcr.io/org",
            dep_hash="dep1",
            tag="latest",
            pinned_ref=pinned,
            digest="sha256:" + "e" * 64,
            built=True,
        )
    )
    await db_session.commit()

    # Single built row → pinned_ref returned.
    assert await _load_baked_image_ref(db_session, project.id) == pinned

    # An unbuilt row (built=False) for the same project must be ignored.
    db_session.add(
        ProjectComputeImage(
            project_id=project.id,
            image_name="compute-proj",
            registry_repo="ghcr.io/org",
            dep_hash="dep2-unbuilt",
            tag="latest",
            pinned_ref="ghcr.io/org/compute-proj@sha256:" + "f" * 64,
            digest="sha256:" + "f" * 64,
            built=False,
        )
    )
    await db_session.commit()
    assert await _load_baked_image_ref(db_session, project.id) == pinned

    # A newer built row supersedes the older one (ordered by updated_at DESC).
    newer_pinned = "ghcr.io/org/compute-proj@sha256:" + "a" * 64
    db_session.add(
        ProjectComputeImage(
            project_id=project.id,
            image_name="compute-proj",
            registry_repo="ghcr.io/org",
            dep_hash="dep3-newer",
            tag="latest",
            pinned_ref=newer_pinned,
            digest="sha256:" + "a" * 64,
            built=True,
        )
    )
    await db_session.commit()
    assert await _load_baked_image_ref(db_session, project.id) == newer_pinned


# ---------------------------------------------------------------------------
# _stamp_container_image_pin_status: durable per-payload reproducibility flag.
# ---------------------------------------------------------------------------
def test_stamp_container_image_pin_status_mutable():
    from app.compute_experiment_contract import _stamp_container_image_pin_status

    contract = {"container_image": "ghcr.io/org/base:latest"}
    _stamp_container_image_pin_status(contract, "task-1")
    assert contract["container_image_digest_pinned"] is False


def test_stamp_container_image_pin_status_pinned():
    from app.compute_experiment_contract import _stamp_container_image_pin_status

    ref = "ghcr.io/org/base@sha256:" + "e" * 64
    contract = {"container_image": ref}
    _stamp_container_image_pin_status(contract, "task-1")
    assert contract["container_image_digest_pinned"] is True


def test_stamp_container_image_pin_status_no_image_leaves_flag_absent():
    from app.compute_experiment_contract import _stamp_container_image_pin_status

    # A bare bash -lc job declares no image; the flag must stay absent so a
    # "not pinned" signal is never shown for a job that has no image to pin.
    contract = {"command": "python3 -m foo"}
    _stamp_container_image_pin_status(contract, "task-1")
    assert "container_image_digest_pinned" not in contract


@pytest.mark.asyncio
async def test_record_project_compute_image_build_inserts_fresh_row(db_session):
    from app import compute_backends as cb
    from app.models import ProjectComputeImage

    project = await _make_project(db_session)
    digest = "sha256:" + "a" * 64
    result = _build_result(
        pinned_ref=f"ghcr.io/org/repo@{digest}",
        tag="v1",
        dep_hash="hash-fresh",
        built=True,
    )

    row = await cb.record_project_compute_image_build(
        db_session,
        project.id,
        registry_repo="ghcr.io/org/repo",
        image_name="compute-proj",
        result=result,
    )
    await db_session.commit()

    assert row.project_id == project.id
    assert row.registry_repo == "ghcr.io/org/repo"
    assert row.image_name == "compute-proj"
    assert row.dep_hash == "hash-fresh"
    assert row.tag == "v1"
    assert row.pinned_ref == f"ghcr.io/org/repo@{digest}"
    assert row.digest == digest
    assert row.built is True

    rows = (
        await db_session.execute(
            select(ProjectComputeImage).where(
                ProjectComputeImage.project_id == project.id
            )
        )
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_record_project_compute_image_build_upserts(db_session):
    """A second build for the same (project_id, dep_hash) updates in place."""
    from app import compute_backends as cb
    from app.models import ProjectComputeImage

    project = await _make_project(db_session)

    first_digest = "sha256:" + "b" * 64
    first = await cb.record_project_compute_image_build(
        db_session,
        project.id,
        registry_repo="ghcr.io/org/repo",
        image_name="compute-proj",
        result=_build_result(
            pinned_ref=f"ghcr.io/org/repo@{first_digest}",
            tag="v1",
            dep_hash="stable-hash",
            built=True,
        ),
    )
    await db_session.commit()
    first_id = first.id

    # Second build: same dep_hash, different digest/tag/built -> UPSERT in place.
    second_digest = "sha256:" + "c" * 64
    second = await cb.record_project_compute_image_build(
        db_session,
        project.id,
        registry_repo="ghcr.io/org/repo",
        image_name="compute-proj",
        result=_build_result(
            pinned_ref=f"ghcr.io/org/repo@{second_digest}",
            tag="v2",
            dep_hash="stable-hash",
            built=False,
        ),
    )
    await db_session.commit()

    # No duplicate row: same primary key, updated fields.
    assert second.id == first_id
    assert second.pinned_ref == f"ghcr.io/org/repo@{second_digest}"
    assert second.digest == second_digest
    assert second.tag == "v2"
    assert second.built is False

    rows = (
        await db_session.execute(
            select(ProjectComputeImage).where(
                ProjectComputeImage.project_id == project.id
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == first_id
    assert rows[0].tag == "v2"


@pytest.mark.asyncio
async def test_record_project_compute_image_build_parses_digest(db_session):
    """Digest is parsed from a @sha256 ref and None for a tag-only ref."""
    from app import compute_backends as cb

    project = await _make_project(db_session)

    digest = "sha256:" + "d" * 64
    pinned = await cb.record_project_compute_image_build(
        db_session,
        project.id,
        registry_repo="ghcr.io/org/repo",
        image_name="compute-proj",
        result=_build_result(
            pinned_ref=f"ghcr.io/org/repo@{digest}",
            dep_hash="digest-pinned",
        ),
    )
    await db_session.commit()
    assert pinned.digest == digest

    tag_only = await cb.record_project_compute_image_build(
        db_session,
        project.id,
        registry_repo="ghcr.io/org/repo",
        image_name="compute-proj",
        result=_build_result(
            pinned_ref="ghcr.io/org/repo:latest",
            tag="latest",
            dep_hash="tag-only",
        ),
    )
    await db_session.commit()
    assert tag_only.digest is None
    assert tag_only.pinned_ref == "ghcr.io/org/repo:latest"


# ---------------------------------------------------------------------------
# record_project_compute_image_build is a REAL upsert (ON CONFLICT DO UPDATE).
#
# It used to be a select-then-insert with no lock: two concurrent build triggers
# for the same (project_id, dep_hash) BOTH missed the existence check, both
# INSERTed, and the loser died on uq_projectcomputeimage_project_dep_hash — a
# 500 out of the build endpoint.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.skipif(
    not USE_POSTGRES,
    reason="ON CONFLICT + a real unique-violation race need PostgreSQL",
)
async def test_record_project_compute_image_build_concurrent_same_dep_hash(db_session):
    """Two CONCURRENT builds for the same (project_id, dep_hash) must not 500.

    Both callers run in their own session, so neither can see the other's
    uncommitted INSERT — exactly the interleaving that made the select-then-insert
    version raise IntegrityError on the unique constraint.
    """
    from app import compute_backends as cb
    from app.models import ProjectComputeImage

    project = await _make_project(db_session)
    project_id = project.id

    async def _record(digest_char: str, tag: str):
        async with TestingSessionLocal() as session:
            row = await cb.record_project_compute_image_build(
                session,
                project_id,
                registry_repo="ghcr.io/org/repo",
                image_name="compute-proj",
                result=_build_result(
                    pinned_ref=f"ghcr.io/org/repo@sha256:{digest_char * 64}",
                    tag=tag,
                    dep_hash="raced-hash",
                    built=True,
                ),
            )
            await session.commit()
            return row.id

    # Concurrent, independent transactions — no IntegrityError may escape.
    ids = await asyncio.gather(_record("e", "v1"), _record("f", "v2"))

    # Exactly ONE row survives, and both callers converged on it.
    rows = (
        await db_session.execute(
            select(ProjectComputeImage).where(
                ProjectComputeImage.project_id == project_id,
                ProjectComputeImage.dep_hash == "raced-hash",
            )
        )
    ).scalars().all()
    assert len(rows) == 1, f"concurrent upsert duplicated the row: {rows}"
    assert ids[0] == ids[1] == rows[0].id


# ---------------------------------------------------------------------------
# resolve_digest_pinned_default_image matches on the image's FULL identity.
#
# Matching on the SHORT image name alone silently resolved a manifest ref to a
# build record that does not describe it: a tag rollback (:v2 -> :v1) kept
# running the rolled-back digest, and an external prebuilt image sharing the
# trailing name hijacked this project's own build record.
# ---------------------------------------------------------------------------
async def _add_build_row(
    db_session,
    project_id,
    *,
    registry_repo,
    image_name,
    tag,
    dep_hash,
    digest,
    updated_at,
):
    from app.models import ProjectComputeImage

    row = ProjectComputeImage(
        project_id=project_id,
        image_name=image_name,
        registry_repo=registry_repo,
        dep_hash=dep_hash,
        tag=tag,
        pinned_ref=f"{registry_repo}/{image_name}@{digest}",
        digest=digest,
        built=True,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    # updated_at has an onupdate=; set it explicitly AFTER the insert so the
    # "most recently built" ordering under test is deterministic.
    row.updated_at = updated_at
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.mark.asyncio
async def test_resolve_digest_pin_tag_rollback_uses_the_rolled_back_build(db_session):
    """Rolling the manifest back :v2 -> :v1 must resolve to the :v1 DIGEST.

    Both builds share registry+image_name, so keying on image_name alone returned
    the most-recently-updated row — the :v2 build the project had just rolled back
    — behind a digest-pinned ref that looked authoritative.
    """
    from app import compute_backends as cb

    project = await _make_project(db_session)
    registry = "us-docker.pkg.dev/proj/compute-images"
    v1_digest = "sha256:" + "1" * 64
    v2_digest = "sha256:" + "2" * 64

    await _add_build_row(
        db_session,
        project.id,
        registry_repo=registry,
        image_name="my-project",
        tag="v1",
        dep_hash="hash-v1",
        digest=v1_digest,
        updated_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    # The NEWER build — this is the one the buggy lookup returned.
    await _add_build_row(
        db_session,
        project.id,
        registry_repo=registry,
        image_name="my-project",
        tag="v2",
        dep_hash="hash-v2",
        digest=v2_digest,
        updated_at=datetime(2026, 6, 1, 0, 0, 0),
    )

    resolved = await cb.resolve_digest_pinned_default_image(
        db_session, project.id, f"{registry}/my-project:v1"
    )

    assert resolved == f"{registry}/my-project@{v1_digest}", (
        "Rolling back to :v1 resolved to the :v2 digest — the project silently "
        f"runs the image it rolled back. got={resolved!r}"
    )
    assert v2_digest not in (resolved or "")

    # And :v2 still resolves to :v2 (no over-correction).
    assert await cb.resolve_digest_pinned_default_image(
        db_session, project.id, f"{registry}/my-project:v2"
    ) == f"{registry}/my-project@{v2_digest}"


@pytest.mark.asyncio
async def test_resolve_digest_pin_ignores_foreign_registry_with_same_name(db_session):
    """An EXTERNAL image sharing the trailing name must not match a local build.

    Switching to a prebuilt ghcr.io/vendor/my-project used to match this project's
    own …/compute-images/my-project build record and run THAT digest. The foreign
    ref must pass through untouched.
    """
    from app import compute_backends as cb

    project = await _make_project(db_session)
    registry = "us-docker.pkg.dev/proj/compute-images"
    local_digest = "sha256:" + "9" * 64

    await _add_build_row(
        db_session,
        project.id,
        registry_repo=registry,
        image_name="my-project",
        tag="latest",
        dep_hash="hash-local",
        digest=local_digest,
        updated_at=datetime(2026, 6, 1, 0, 0, 0),
    )

    foreign = "ghcr.io/vendor/my-project:latest"
    resolved = await cb.resolve_digest_pinned_default_image(
        db_session, project.id, foreign
    )

    assert resolved == foreign, (
        "An external image sharing the trailing name resolved to THIS project's "
        f"build digest. got={resolved!r}"
    )
    assert local_digest not in (resolved or "")


@pytest.mark.asyncio
async def test_resolve_digest_pin_floating_tag_still_pins_latest_build(db_session):
    """A floating tag (:latest) still pins to the latest build for that image.

    The build pipeline pushes content-hash tags, never `latest`, so no per-tag row
    exists for a floating tag by construction — the latest build IS the right
    answer, and this is the primary use case the feature exists for.
    """
    from app import compute_backends as cb

    project = await _make_project(db_session)
    registry = "us-docker.pkg.dev/proj/compute-images"
    old_digest = "sha256:" + "a" * 64
    new_digest = "sha256:" + "b" * 64

    await _add_build_row(
        db_session,
        project.id,
        registry_repo=registry,
        image_name="my-project",
        tag="deadbeefcafe0001",  # a dep-hash tag, as the pipeline pushes
        dep_hash="deadbeefcafe0001",
        digest=old_digest,
        updated_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    await _add_build_row(
        db_session,
        project.id,
        registry_repo=registry,
        image_name="my-project",
        tag="deadbeefcafe0002",
        dep_hash="deadbeefcafe0002",
        digest=new_digest,
        updated_at=datetime(2026, 6, 1, 0, 0, 0),
    )

    resolved = await cb.resolve_digest_pinned_default_image(
        db_session, project.id, f"{registry}/my-project:latest"
    )
    assert resolved == f"{registry}/my-project@{new_digest}"


def test_split_image_ref_decomposes_registry_name_and_tag():
    from app import compute_backends as cb

    assert cb._split_image_ref("us-docker.pkg.dev/p/ci/my-project:v2") == (
        "us-docker.pkg.dev/p/ci",
        "my-project",
        "v2",
    )
    # A registry :port must survive; only the FINAL segment carries the tag.
    assert cb._split_image_ref("localhost:5000/img:v2") == ("localhost:5000", "img", "v2")
    assert cb._split_image_ref("ghcr.io/org/base") == ("ghcr.io/org", "base", "")
    assert cb._split_image_ref("bare-image") == ("", "bare-image", "")
    assert cb._split_image_ref("") == ("", "", "")


# ---------------------------------------------------------------------------
# resolve_compute_image_registry_repo — pure env/config function.
# ---------------------------------------------------------------------------


def test_resolve_compute_image_registry_repo_explicit_env(monkeypatch):
    """COMPUTE_IMAGE_REGISTRY env var takes precedence."""
    from app import compute_backends as cb

    monkeypatch.setenv("COMPUTE_IMAGE_REGISTRY", "us-docker.pkg.dev/my-project/compute-images")
    assert cb.resolve_compute_image_registry_repo() == "us-docker.pkg.dev/my-project/compute-images"


def test_resolve_compute_image_registry_repo_fallback(monkeypatch):
    """Without COMPUTE_IMAGE_REGISTRY, falls back to derived form from GCP_PROJECT_ID."""
    from app import compute_backends as cb
    from app import storage

    monkeypatch.delenv("COMPUTE_IMAGE_REGISTRY", raising=False)
    monkeypatch.delenv("COMPUTE_IMAGE_REGION", raising=False)
    monkeypatch.setattr(storage, "GCP_PROJECT_ID", "my-gcp-proj")
    result = cb.resolve_compute_image_registry_repo()
    assert result == "us-docker.pkg.dev/my-gcp-proj/compute-images"


def test_resolve_compute_image_registry_repo_custom_region(monkeypatch):
    """COMPUTE_IMAGE_REGION customises the region prefix."""
    from app import compute_backends as cb
    from app import storage

    monkeypatch.delenv("COMPUTE_IMAGE_REGISTRY", raising=False)
    monkeypatch.setenv("COMPUTE_IMAGE_REGION", "europe-west4")
    monkeypatch.setattr(storage, "GCP_PROJECT_ID", "my-gcp-proj")
    result = cb.resolve_compute_image_registry_repo()
    assert result == "europe-west4-docker.pkg.dev/my-gcp-proj/compute-images"


# ---------------------------------------------------------------------------
# POST /projects/{project_id}/compute-image/build — trigger endpoint.
#
# Exercises the seam end-to-end: build_project_compute_image is monkeypatched
# to skip the real Docker build; the endpoint must persist a ProjectComputeImage
# row with the parsed digest from the returned BuildResult.
# ---------------------------------------------------------------------------


async def _make_build_trigger_project(db_session, name="Build-Trigger-Test", role="owner"):
    """Project + membership for the compute-image build trigger.

    The trigger is OWNER-grade: it runs `docker build`/`docker push` on the
    ORCHESTRATOR HOST and pushes to the shared compute-image registry. It used to
    accept any project MEMBER, which is the privilege half of the OOM finding —
    a member could fan out unbounded multi-GB docker builds on the API process.
    ``role`` is parameterised so the denial path is covered too.
    """
    from app.models import Project, ProjectMember

    project = Project(name=name, user_id="test-user-123")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(ProjectMember(project_id=project.id, user_id="test-user-123", role=role))
    await db_session.commit()
    return project


@pytest.mark.asyncio
async def test_project_compute_image_build_trigger_persists_digest(
    client, db_session, monkeypatch
):
    """POST /projects/{id}/compute-image/build persists a ProjectComputeImage row.

    Acceptance test: monkeypatches build_project_compute_image to return a
    digest-pinned BuildResult; asserts the row is persisted with the parsed
    sha256 digest.
    """
    from app import compute_backends as cb
    from app.compute_image_build import BuildResult
    from app.models import ProjectComputeImage
    from app.routers import admin_project_tools as pt

    project = await _make_build_trigger_project(db_session)
    digest = "sha256:" + "b" * 64
    pinned_ref = f"us-docker.pkg.dev/test-project/compute-images/compute-proj@{digest}"

    fake_result = BuildResult(
        pinned_ref=pinned_ref,
        tag="dep-abc123",
        dep_hash="abc123",
        already_present=False,
        built=True,
    )

    # Monkeypatch the manifest discovery to return a valid compute.image.build config.
    async def _fake_discover(proj):
        return {
            "tools": [],
            "skills": [],
            "compute": {
                "image": {
                    "name": "us-docker.pkg.dev/test-project/compute-images/compute-proj:dep-abc123",
                    "build": {
                        "dockerfile": ".blueprint/Dockerfile.compute",
                        "parent": None,
                        "build_args": {},
                    },
                }
            },
            "error": None,
        }

    monkeypatch.setattr(pt, "_discover_manifest_tools", _fake_discover)

    # Monkeypatch the build function to return the fake result without a real build.
    async def _fake_build(proj, *, registry_repo, build_config, dry_run=False, **kwargs):
        return fake_result

    monkeypatch.setattr(cb, "build_project_compute_image", _fake_build)
    monkeypatch.setenv("COMPUTE_IMAGE_REGISTRY", "us-docker.pkg.dev/test-project/compute-images")

    resp = await client.post(f"/api/admin/projects/{project.id}/compute-image/build")
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["digest"] == digest
    assert body["pinned_ref"] == pinned_ref
    assert body["dep_hash"] == "abc123"
    assert body["built"] is True
    assert body["already_present"] is False

    # Verify the row was persisted in the database.
    row = (
        await db_session.execute(
            select(ProjectComputeImage).where(
                ProjectComputeImage.project_id == project.id,
                ProjectComputeImage.dep_hash == "abc123",
            )
        )
    ).scalar_one_or_none()
    assert row is not None
    assert row.digest == digest
    assert row.pinned_ref == pinned_ref


@pytest.mark.asyncio
async def test_project_compute_image_build_no_build_config_returns_422(
    client, db_session, monkeypatch
):
    """POST returns 422 when the project declares no compute.image.build section."""
    from app import compute_backends as cb
    from app.models import ProjectComputeImage
    from app.routers import admin_project_tools as pt

    project = await _make_build_trigger_project(db_session, name="NoBuild-Test")

    # Manifest has image.name but no build section — resolve returns None.
    async def _fake_discover_no_build(proj):
        return {
            "tools": [],
            "skills": [],
            "compute": {
                "image": {
                    "name": "prebuilt-image:latest",
                    # No "build" key — resolve_project_compute_image_build returns None.
                }
            },
            "error": None,
        }

    monkeypatch.setattr(pt, "_discover_manifest_tools", _fake_discover_no_build)

    build_called = []

    async def _fail_if_called(*args, **kwargs):
        build_called.append(True)
        raise AssertionError("build should not be called")

    monkeypatch.setattr(cb, "build_project_compute_image", _fail_if_called)

    resp = await client.post(f"/api/admin/projects/{project.id}/compute-image/build")
    assert resp.status_code == 422
    # The app installs a global HTTPException handler (app/main.py) that reshapes
    # every error body to {"error": {"code", "message"}} — there is no "detail" key.
    assert "build" in resp.json()["error"]["message"].lower()

    # No row should have been persisted.
    rows = (
        await db_session.execute(
            select(ProjectComputeImage).where(
                ProjectComputeImage.project_id == project.id
            )
        )
    ).scalars().all()
    assert rows == []
    assert build_called == []  # build was never invoked


@pytest.mark.asyncio
async def test_project_compute_image_build_trigger_denies_member(
    client, db_session, monkeypatch
):
    """A project MEMBER cannot trigger the build — it is owner-grade.

    The trigger shells out to `docker build`/`docker push` ON THE ORCHESTRATOR
    HOST and publishes to the shared registry. Leaving that member-reachable let
    any member fan out unbounded multi-GB builds against the API process.
    """
    from app import compute_backends as cb
    from app.routers import admin_project_tools as pt

    project = await _make_build_trigger_project(
        db_session, name="Member-Denied", role="member"
    )

    async def _fail_if_called(*args, **kwargs):
        raise AssertionError("build must not be reachable by a member")

    monkeypatch.setattr(cb, "build_project_compute_image", _fail_if_called)
    monkeypatch.setattr(pt, "_discover_manifest_tools", _fail_if_called)

    resp = await client.post(f"/api/admin/projects/{project.id}/compute-image/build")
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# DB-aware GC guard for project compute-image digests.
#
# Registry retention (Terraform cleanup_policy) is purely time/count-based and
# cannot query the DB, so a digest a live/recent compute job depends on can be
# GC'd out from under it. These cover the DB-aware primitive
# (referenced_compute_image_digests), the fail-open registry-tag helpers, and
# the reaper sweep that ties them together
# (sweep_protect_referenced_compute_image_digests).
# ---------------------------------------------------------------------------
def _digest(hexchar):
    return "sha256:" + hexchar * 64


async def _make_compute_job(
    db_session, project, *, status, container_image=None, completed_at=None
):
    meta = {}
    if container_image is not None:
        meta["container_image"] = container_image
    job = ComputeJob(
        project_id=project.id,
        backend="gcp",
        status=status,
        polling_metadata=meta,
        completed_at=completed_at,
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


@pytest.mark.asyncio
async def test_referenced_compute_image_digests_active_and_recent_only(db_session):
    project = await _make_project(db_session, name="GC Guard Refs")
    repo = "ghcr.io/org/img"
    d_active = _digest("a")
    d_recent = _digest("b")
    d_old = _digest("c")
    d_tag_only = "ghcr.io/org/img:latest"  # no @digest -> ignored

    now = _utcnow()
    # Live job -> referenced regardless of since.
    await _make_compute_job(
        db_session,
        project,
        status=ComputeJobStatus.RUNNING,
        container_image=f"{repo}@{d_active}",
    )
    # Terminal job completed inside the window -> referenced.
    await _make_compute_job(
        db_session,
        project,
        status=ComputeJobStatus.SUCCEEDED,
        container_image=f"{repo}@{d_recent}",
        completed_at=now - timedelta(days=2),
    )
    # Terminal job completed long ago -> NOT referenced.
    await _make_compute_job(
        db_session,
        project,
        status=ComputeJobStatus.SUCCEEDED,
        container_image=f"{repo}@{d_old}",
        completed_at=now - timedelta(days=90),
    )
    # Live job with only a tag (no digest) -> contributes nothing.
    await _make_compute_job(
        db_session,
        project,
        status=ComputeJobStatus.QUEUED,
        container_image=d_tag_only,
    )

    cutoff = now - timedelta(days=30)
    referenced = await compute_jobs.referenced_compute_image_digests(
        db_session, since=cutoff
    )
    assert referenced == {d_active, d_recent}

    # With no window, only live jobs count.
    live_only = await compute_jobs.referenced_compute_image_digests(db_session)
    assert live_only == {d_active}


def test_registry_protection_tag_helpers_are_fail_open():
    from app import compute_image_build as cib

    digest = _digest("a")
    assert cib.protection_tag_for_digest(digest) == "inuse-" + "a" * 16

    # apply: success path returns the tag; non-zero exit and OSError fail open.
    ok = cib.apply_registry_protection_tag(
        "ghcr.io/org/img", digest, _run=lambda *a, **k: MagicMock(returncode=0)
    )
    assert ok == "inuse-" + "a" * 16
    assert (
        cib.apply_registry_protection_tag(
            "ghcr.io/org/img", digest, _run=lambda *a, **k: MagicMock(returncode=1)
        )
        is None
    )

    def _boom(*a, **k):
        raise OSError("gcloud missing")

    assert (
        cib.apply_registry_protection_tag("ghcr.io/org/img", digest, _run=_boom)
        is None
    )
    assert cib.list_registry_protection_tags("ghcr.io/org/img", _run=_boom) == []
    assert cib.delete_registry_protection_tag("ghcr.io/org/img", "inuse-x", _run=_boom) is False

    # list: parses the short inuse- tag name out of gcloud JSON, drops others.
    tags_json = json.dumps(
        [
            {"tag": "ghcr.io/org/img:inuse-aaaaaaaaaaaaaaaa"},
            {"tag": "ghcr.io/org/img:latest"},
            {"tag": "ghcr.io/org/img:inuse-bbbbbbbbbbbbbbbb"},
        ]
    )
    listed = cib.list_registry_protection_tags(
        "ghcr.io/org/img",
        _run=lambda *a, **k: MagicMock(returncode=0, stdout=tags_json),
    )
    assert set(listed) == {"inuse-" + "a" * 16, "inuse-" + "b" * 16}


@pytest.mark.asyncio
async def test_compute_image_gc_protects_referenced_digest(db_session):
    """The reaper sweep tags a referenced digest inuse- and prunes stale tags."""
    from app import reaper_sweeps

    # Reset the module-level cadence timer so the first call below is due.
    reaper_sweeps._last_compute_image_gc_guard_sweep = None

    project = await _make_project(db_session, name="GC Guard Sweep")
    repo_base = "ghcr.io/org"
    image_name = "img"
    full_repo = f"{repo_base}/{image_name}"
    d_referenced = _digest("a")
    # A genuinely orphaned digest: it has NO ProjectComputeImage row and no
    # live/recent job — only a leftover ``inuse-`` tag in the registry. (A digest
    # that DID have a ProjectComputeImage row would be a standing pin and stay
    # protected regardless of age — see test_reaper_sweeps_compute_image.py.)
    d_unreferenced = _digest("d")

    # One baked image, still referenced by a running job.
    db_session.add(
        ProjectComputeImage(
            project_id=project.id,
            image_name=image_name,
            registry_repo=repo_base,
            dep_hash="dep-ref",
            tag="tag-ref",
            pinned_ref=f"{full_repo}@{d_referenced}",
            digest=d_referenced,
            built=True,
        )
    )
    await db_session.commit()

    # A live job references the digest.
    await _make_compute_job(
        db_session,
        project,
        status=ComputeJobStatus.RUNNING,
        container_image=f"{full_repo}@{d_referenced}",
    )

    # Run the sweep from a point 60 days in the future: the image row was just
    # built, so pushing "now" past the 30-day freshness window shows protection
    # holds via BOTH the standing pin and the live job's reference, while the
    # orphaned registry tag (no row, no job) is correctly pruned.
    sweep_now = _utcnow() + timedelta(days=60)

    applied_tags = []
    deleted_tags = []
    protect_tag = reaper_sweeps.sweep_protect_referenced_compute_image_digests

    def _apply(repo, digest):
        tag = "inuse-" + digest.split(":", 1)[-1][:16]
        applied_tags.append((repo, tag))
        return tag

    # Registry already carries a stale inuse- tag for the now-unreferenced digest.
    stale_tag = "inuse-" + d_unreferenced.split(":", 1)[-1][:16]

    def _list(repo):
        return [
            "inuse-" + d_referenced.split(":", 1)[-1][:16],
            stale_tag,
        ]

    def _delete(repo, tag):
        deleted_tags.append((repo, tag))
        return True

    summary = await protect_tag(
        db_session,
        sweep_now,
        _apply_tag=_apply,
        _list_tags=_list,
        _delete_tag=_delete,
    )

    # The referenced digest was protected; the orphan was not.
    referenced_tag = "inuse-" + d_referenced.split(":", 1)[-1][:16]
    assert (full_repo, referenced_tag) in applied_tags
    assert (full_repo, stale_tag) not in applied_tags
    assert summary["applied"] == 1

    # The stale tag for the unreferenced digest was pruned; the live one kept.
    assert (full_repo, stale_tag) in deleted_tags
    assert (full_repo, referenced_tag) not in deleted_tags
    assert summary["pruned"] == 1

    # Cadence gate: an immediate second call is a no-op.
    second = await protect_tag(
        db_session,
        sweep_now,
        _apply_tag=_apply,
        _list_tags=_list,
        _delete_tag=_delete,
    )
    assert second["skipped_cadence"] == 1


@pytest.mark.asyncio
async def test_gc_guard_sweep_never_raises_when_registry_helper_throws(db_session):
    from app import reaper_sweeps

    # Reset the module-level cadence timer so this call is due.
    reaper_sweeps._last_compute_image_gc_guard_sweep = None

    project = await _make_project(db_session, name="GC Guard Robust")
    full_repo = "ghcr.io/org/img"
    d = _digest("e")
    db_session.add(
        ProjectComputeImage(
            project_id=project.id,
            image_name="img",
            registry_repo="ghcr.io/org",
            dep_hash="dep-e",
            tag="tag-e",
            pinned_ref=f"{full_repo}@{d}",
            digest=d,
            built=True,
        )
    )
    await db_session.commit()
    await _make_compute_job(
        db_session,
        project,
        status=ComputeJobStatus.RUNNING,
        container_image=f"{full_repo}@{d}",
    )

    def _boom(*a, **k):
        raise RuntimeError("registry exploded")

    # A throwing list helper must not propagate out of the sweep.
    summary = await reaper_sweeps.sweep_protect_referenced_compute_image_digests(
        db_session,
        _utcnow(),
        _apply_tag=lambda repo, digest: "inuse-x",
        _list_tags=_boom,
        _delete_tag=lambda repo, tag: True,
    )
    assert summary["applied"] == 1
    assert summary["pruned"] == 0


# --------------------------------------------------------------------------
# Multi-arm viewable-plot collision: same-named plots across arm subdirs must
# NOT collapse to a single object (regression for basename-only dedupe).
# --------------------------------------------------------------------------

# Minimal magic-byte-prefixed blobs; extension classification drives selection.
_VIEW_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"


def _make_viewable_tar_gz(files: dict[str, bytes]) -> bytes:
    """In-memory ``.tar.gz`` whose members mirror ``tar -C dir -czf - .`` naming."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_sanitize_viewable_relpath_preserves_arm_subdir_context():
    # Same basename under distinct arm subdirs -> distinct, non-colliding names.
    assert storage.sanitize_viewable_relpath("arm1/reward.png") == "arm1__reward.png"
    assert storage.sanitize_viewable_relpath("arm2/reward.png") == "arm2__reward.png"
    # Root-level file keeps its bare name (no spurious joiner).
    assert storage.sanitize_viewable_relpath("reward.png") == "reward.png"
    # Leading ``./`` and unsafe chars are normalized; nesting is folded.
    assert storage.sanitize_viewable_relpath("./sweep/arm 1/fig.png") == "sweep__arm_1__fig.png"
    # Absolute path and parent traversal are REJECTED, not silently flattened.
    assert storage.sanitize_viewable_relpath("/etc/passwd.png") is None
    assert storage.sanitize_viewable_relpath("../secrets/x.png") is None
    assert storage.sanitize_viewable_relpath("arm1/../../x.png") is None
    # Degenerate inputs.
    assert storage.sanitize_viewable_relpath("") is None
    assert storage.sanitize_viewable_relpath("./") is None
    assert storage.sanitize_viewable_relpath(None) is None  # type: ignore[arg-type]


def test_extract_viewable_candidates_keeps_multi_arm_same_basename_from_bundle():
    """A sweep whose arms each write reward.png must yield one object PER arm."""
    bundle = _make_viewable_tar_gz(
        {
            "./arm1/reward.png": _VIEW_PNG_BYTES + b"-a1",
            "./arm2/reward.png": _VIEW_PNG_BYTES + b"-a2",
            "./arm3/reward.png": _VIEW_PNG_BYTES + b"-a3",
        }
    )
    cands = compute_backends_module._extract_viewable_candidates_from_bundle(bundle)
    names = sorted(c.filename for c in cands)
    # All three arms survive as distinct, individually-addressable objects
    # (previously collapsed to a single "reward.png").
    assert names == ["arm1__reward.png", "arm2__reward.png", "arm3__reward.png"]
    # Each candidate carries its OWN arm's bytes, not the first arm's.
    by_name = {c.filename: c for c in cands}
    assert by_name["arm1__reward.png"].data == _VIEW_PNG_BYTES + b"-a1"
    assert by_name["arm2__reward.png"].data == _VIEW_PNG_BYTES + b"-a2"
    # Distinct object names -> distinct durable URIs (no bucket overwrite).
    jid = uuid.uuid4()
    uris = {storage.compute_job_viewable_uri(jid, c.filename) for c in cands}
    assert len(uris) == 3


def test_collect_viewable_candidates_keeps_multi_arm_same_basename_from_dir(tmp_path):
    """On-disk (local backend) analogue: per-arm subdirs stay distinct."""
    for arm in ("arm1", "arm2"):
        arm_dir = tmp_path / arm
        arm_dir.mkdir()
        (arm_dir / "reward.png").write_bytes(_VIEW_PNG_BYTES + arm.encode())
    # A root-level file keeps its bare name alongside the nested ones.
    (tmp_path / "summary.png").write_bytes(_VIEW_PNG_BYTES + b"-root")

    cands, _size_dropped, _svg_count = compute_backends_module._collect_viewable_candidates_from_dir(
        str(tmp_path)
    )
    names = sorted(c.filename for c in cands)
    assert names == ["arm1__reward.png", "arm2__reward.png", "summary.png"]
    by_name = {c.filename: c for c in cands}
    assert by_name["arm1__reward.png"].data == _VIEW_PNG_BYTES + b"arm1"
    assert by_name["arm2__reward.png"].data == _VIEW_PNG_BYTES + b"arm2"


def test_collect_viewable_candidates_from_dir_streams_large_video(tmp_path):
    """A local video over the in-memory image cap becomes a STREAMED candidate.

    Instead of being read into memory (and rejected by the image cap), an
    oversized local video yields a candidate carrying a ``source_path`` (the
    on-disk original) with no in-memory ``data`` and the ``artifact_root`` set so
    the upload path can re-verify symlink containment. A co-located image stays
    on the in-memory ``data`` path.
    """
    image_cap = compute_backends_module._MAX_VIEWABLE_FILE_BYTES
    big_video = os.urandom(image_cap + 256 * 1024)  # ~8.25 MiB > image cap
    (tmp_path / "rollout.mp4").write_bytes(big_video)
    (tmp_path / "frame.png").write_bytes(_VIEW_PNG_BYTES)

    cands, _size_dropped, _svg_count = compute_backends_module._collect_viewable_candidates_from_dir(
        str(tmp_path)
    )
    by_name = {c.filename: c for c in cands}
    assert set(by_name) == {"rollout.mp4", "frame.png"}

    video = by_name["rollout.mp4"]
    assert video.kind == "video"
    assert video.data is None
    assert video.source_path == str(tmp_path / "rollout.mp4")
    assert video.source_is_temp is False
    assert video.artifact_root == os.path.realpath(str(tmp_path))
    assert video.size_bytes == len(big_video)

    image = by_name["frame.png"]
    assert image.data == _VIEW_PNG_BYTES
    assert image.source_path is None


@pytest.mark.asyncio
async def test_persist_streams_local_video_via_nofollow_open(tmp_path, monkeypatch):
    """A local job's oversized video is streamed to storage via an O_NOFOLLOW open.

    End-to-end for the local backend: the streamed candidate's original file is
    opened (not buffered) and handed to ``upload_compute_job_viewable_stream``;
    the original artifact file is left in place (never unlinked as a temp).
    """
    image_cap = compute_backends_module._MAX_VIEWABLE_FILE_BYTES
    big_video = os.urandom(image_cap + 256 * 1024)
    video_path = tmp_path / "rollout.mp4"
    video_path.write_bytes(big_video)

    job = MagicMock()
    job.id = uuid.uuid4()
    job.backend = compute_backends_module.LOCAL_BACKEND_NAME
    job.polling_metadata = {"artifact_dir": str(tmp_path)}

    streamed: list = []

    async def _fake_stream_upload(job_id, basename, fileobj, content_type, *, size=None):
        fileobj.seek(0, os.SEEK_END)
        measured = fileobj.tell()
        fileobj.seek(0)
        streamed.append({"basename": basename, "size": measured, "size_kwarg": size})
        return storage.compute_job_viewable_uri(job_id, basename)

    async def _forbid_file_upload(*_a, **_k):
        raise AssertionError("a large video must stream, not use the in-memory seam")

    monkeypatch.setattr(
        compute_backends_module.storage,
        "upload_compute_job_viewable_stream",
        _fake_stream_upload,
    )
    monkeypatch.setattr(
        compute_backends_module.storage,
        "upload_compute_job_viewable_file",
        _forbid_file_upload,
    )

    manifest, dropped = await compute_backends_module.persist_viewable_artifacts(job)

    assert dropped == 0
    assert [m["filename"] for m in manifest] == ["rollout.mp4"]
    assert manifest[0]["size_bytes"] == len(big_video)
    assert streamed == [
        {"basename": "rollout.mp4", "size": len(big_video), "size_kwarg": len(big_video)}
    ]
    # The ORIGINAL artifact file must remain in place (not treated as a temp).
    assert video_path.exists()


# ---------------------------------------------------------------------------
# Dropped-file recording: cap enforcement, deterministic ordering, tuple return
# ---------------------------------------------------------------------------

_MAX_COUNT = compute_backends_module._MAX_VIEWABLE_FILE_COUNT


def test_extract_viewable_candidates_collects_beyond_cap_for_determinism():
    """Bundle extractor collects ALL qualifying files (count cap removed from inner loop).

    The cap is applied by _collect_viewable_artifact_candidates, not here, so
    _extract_viewable_candidates_from_bundle returns every qualifying file.
    """
    files = {f"./file{i:03d}.png": _VIEW_PNG_BYTES + str(i).encode() for i in range(_MAX_COUNT + 5)}
    bundle = _make_viewable_tar_gz(files)
    cands = compute_backends_module._extract_viewable_candidates_from_bundle(bundle)
    # Without the inner cap, all _MAX_COUNT+5 qualifying files are returned.
    assert len(cands) == _MAX_COUNT + 5


def test_collect_viewable_candidates_from_dir_collects_beyond_cap(tmp_path):
    """Dir walker collects ALL qualifying files (count cap removed from inner loop)."""
    for i in range(_MAX_COUNT + 3):
        (tmp_path / f"file{i:03d}.png").write_bytes(_VIEW_PNG_BYTES + str(i).encode())
    cands, size_dropped, _svg_count = compute_backends_module._collect_viewable_candidates_from_dir(
        str(tmp_path)
    )
    assert len(cands) == _MAX_COUNT + 3
    assert size_dropped == 0


@pytest.mark.asyncio
async def test_collect_viewable_artifact_candidates_sorts_and_caps(tmp_path):
    """_collect_viewable_artifact_candidates sorts by filename, caps at MAX, returns dropped count."""
    # Create files whose names sort in a different order than os.walk may return.
    names = [f"zzz{i:02d}.png" for i in range(5)] + [f"aaa{i:02d}.png" for i in range(5)]
    for name in names:
        (tmp_path / name).write_bytes(_VIEW_PNG_BYTES + name.encode())

    job = MagicMock()
    job.backend = compute_backends_module.LOCAL_BACKEND_NAME
    job.polling_metadata = {"artifact_dir": str(tmp_path)}
    job.external_compute_id = None

    with patch.object(
        compute_backends_module,
        "_MAX_VIEWABLE_FILE_COUNT",
        7,
    ):
        selected, dropped, _svg_count = await compute_backends_module._collect_viewable_artifact_candidates(job)

    # 10 total, cap=7 → 3 dropped
    assert len(selected) == 7
    assert dropped == 3
    # Selected must be the lexicographically first 7 (aaa00..aaa04, zzz00, zzz01)
    selected_names = [c.filename for c in selected]
    assert selected_names == sorted(selected_names)
    assert selected_names[0].startswith("aaa")


@pytest.mark.asyncio
async def test_collect_viewable_artifact_candidates_no_drop_when_within_cap(tmp_path):
    """When file count ≤ cap, dropped == 0."""
    for i in range(3):
        (tmp_path / f"img{i}.png").write_bytes(_VIEW_PNG_BYTES + str(i).encode())

    job = MagicMock()
    job.backend = compute_backends_module.LOCAL_BACKEND_NAME
    job.polling_metadata = {"artifact_dir": str(tmp_path)}
    job.external_compute_id = None

    selected, dropped, _svg_count = await compute_backends_module._collect_viewable_artifact_candidates(job)
    assert len(selected) == 3
    assert dropped == 0


@pytest.mark.asyncio
async def test_persist_viewable_artifacts_returns_tuple_with_dropped_count(tmp_path):
    """persist_viewable_artifacts returns (manifest, dropped) not a bare list."""
    for i in range(_MAX_COUNT + 2):
        (tmp_path / f"img{i:03d}.png").write_bytes(_VIEW_PNG_BYTES + str(i).encode())

    job = MagicMock()
    job.id = uuid.uuid4()
    job.backend = compute_backends_module.LOCAL_BACKEND_NAME
    job.polling_metadata = {"artifact_dir": str(tmp_path)}
    job.external_compute_id = None

    async def _fake_upload(job_id, filename, data, content_type):
        return f"gs://fake-bucket/compute-jobs/{job_id}/artifacts/files/{filename}"

    with patch.object(
        compute_backends_module.storage,
        "upload_compute_job_viewable_file",
        side_effect=_fake_upload,
    ):
        result = await compute_backends_module.persist_viewable_artifacts(job)

    assert isinstance(result, tuple) and len(result) == 2
    manifest, dropped = result
    assert len(manifest) == _MAX_COUNT
    assert dropped == 2
    # Each manifest entry has the required fields
    entry = manifest[0]
    assert {
        "filename",
        "storage_uri",
        "content_type",
        "size_bytes",
        "kind",
        "revision",
        "live",
    } <= entry.keys()
    assert isinstance(entry["revision"], str) and entry["revision"]
    assert entry["live"] is False


@pytest.mark.asyncio
async def test_persist_viewable_artifacts_returns_zero_dropped_for_small_set(tmp_path):
    """When files ≤ cap, persist_viewable_artifacts returns dropped == 0."""
    (tmp_path / "plot.png").write_bytes(_VIEW_PNG_BYTES + b"-only")

    job = MagicMock()
    job.id = uuid.uuid4()
    job.backend = compute_backends_module.LOCAL_BACKEND_NAME
    job.polling_metadata = {"artifact_dir": str(tmp_path)}
    job.external_compute_id = None

    async def _fake_upload(job_id, filename, data, content_type):
        return f"gs://fake/{filename}"

    with patch.object(
        compute_backends_module.storage,
        "upload_compute_job_viewable_file",
        side_effect=_fake_upload,
    ):
        manifest, dropped = await compute_backends_module.persist_viewable_artifacts(job)

    assert dropped == 0
    assert len(manifest) == 1


def test_extract_viewable_candidates_from_bundle_deterministic_order():
    """Bundle extractor returns files in the order they appear in the archive.

    Deterministic selection (alphabetical) is applied upstream by
    _collect_viewable_artifact_candidates; the extractor itself just returns all
    qualifying candidates without reordering.
    """
    # Add files to the archive in reverse-alphabetical order.
    files = {
        "./zzz.png": _VIEW_PNG_BYTES + b"-z",
        "./mmm.png": _VIEW_PNG_BYTES + b"-m",
        "./aaa.png": _VIEW_PNG_BYTES + b"-a",
    }
    bundle = _make_viewable_tar_gz(files)
    cands = compute_backends_module._extract_viewable_candidates_from_bundle(bundle)
    assert len(cands) == 3
    names = [c.filename for c in cands]
    # Order matches archive insertion — NOT sorted (sorting is upstream's job)
    assert names == ["zzz.png", "mmm.png", "aaa.png"]


@pytest.mark.asyncio
async def test_process_compute_jobs_once_batch_task_prefetch_cancels_terminal_task_jobs(
    db_session, monkeypatch
):
    """process_compute_jobs_once batch-loads tasks and correctly cancels jobs
    whose owning task is terminal — verifying the N+1 fix doesn't regress behavior.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.database import resolve_async_engine

    project = Project(name="Batch Prefetch Project", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Two tasks: one terminal (DONE), one still in progress.
    task_done = Task(
        project_id=project.id,
        title="terminal task",
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "compute_experiment"}],
    )
    task_live = Task(
        project_id=project.id,
        title="live task",
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task_done)
    db_session.add(task_live)
    await db_session.commit()
    await db_session.refresh(task_done)
    await db_session.refresh(task_live)

    # A running job owned by the terminal task — should be cancelled.
    job_should_cancel = ComputeJob(
        task_id=task_done.id,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.RUNNING,
        external_compute_id="fake-done-job",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        polling_metadata={},
    )
    # A queued job owned by the live task — should NOT be cancelled.
    job_stays = ComputeJob(
        task_id=task_live.id,
        project_id=project.id,
        backend="local-subprocess",
        status=ComputeJobStatus.QUEUED,
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        polling_metadata={"preflight_smoke_done": True},
    )
    db_session.add(job_should_cancel)
    db_session.add(job_stays)
    await db_session.commit()
    await db_session.refresh(job_should_cancel)
    await db_session.refresh(job_stays)

    class FakeBackend:
        def external_id_for(self, j):
            return "fake-id"

        async def terminate(self, j, *, reason):
            return True

        async def launch(self, spec):
            return ComputeLaunchResult(
                external_id="fake-live-job",
                started_at=_utcnow(),
                cost_rate_per_second=0.0,
                message="launched",
            )

        async def poll_cost(self, j, *, as_of):
            from app.compute_backends import ComputeCostResult
            return ComputeCostResult(total_cost_usd=0.0, measured_at=as_of)

        async def poll_status(self, j):
            return ComputeStatusResult(status=ComputeJobStatus.RUNNING)

    monkeypatch.setattr(compute_jobs, "get_compute_backend", lambda _: FakeBackend())
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job_should_cancel)
    await db_session.refresh(job_stays)

    # The job owned by the terminal task must be cancelled.
    assert job_should_cancel.status == ComputeJobStatus.CANCELLED, (
        f"expected CANCELLED, got {job_should_cancel.status}"
    )
    assert job_should_cancel.outcome == ComputeJobOutcome.CANCELLED

    # The job owned by the live task must still be active (not cancelled).
    assert job_stays.status != ComputeJobStatus.CANCELLED, (
        f"live-task job should not be cancelled but got {job_stays.status}"
    )


# ---------------------------------------------------------------------------
# Tests for the SINGLE viewable-file pipeline (persist_viewable_files_for_job)
# ---------------------------------------------------------------------------
#
# There used to be TWO overlapping viewable pipelines running at every terminal
# transition: the canonical one (`persist_viewable_artifacts` → the
# `artifacts/viewable/` prefix + the `viewable_files` manifest the admin UI reads)
# and a second extractor that downloaded the SAME bundle AGAIN and re-uploaded
# every file to `artifacts/files/` under a DIFFERENT sanitizer and DIFFERENT caps
# (25 MB/50 files vs 8 MiB/25 files). The two copies could therefore disagree
# about which files exist and under what names.
#
# They are now ONE pass. These tests cover that single pipeline: it downloads the
# bundle exactly once, sanitizes once, uploads once, applies ONE cap policy, and
# records the result on BOTH persisted surfaces (the `viewable_files` manifest and
# the `ComputeJobFile` signing-allowlist rows) from that single pass — so the two
# surfaces cannot drift apart.


def _make_viewable_tar_bundle(files: dict[str, bytes]) -> bytes:
    """Thin wrapper around the shared ``_make_tar_gz_bundle`` for readable tests."""
    return _make_tar_gz_bundle(files)


async def _create_bundle_compute_job(db_session, backend: str = "gcp") -> ComputeJob:
    """Create a minimal Project + ComputeJob on a bundle-producing backend.

    A real ``ComputeJob`` (not just its id) is required because the single pipeline
    is backend-aware: it reads the durable bundle for gcp/runner-subprocess jobs and
    walks the on-disk artifact dir for local ones.
    """
    project = Project(name=f"vf-test-{uuid.uuid4().hex[:8]}", user_id="test")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    job = ComputeJob(project_id=project.id, backend=backend)
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


def _mock_viewable_storage(monkeypatch, bundle: bytes, *, fail_on: str | None = None):
    """Point the single viewable pipeline at an in-memory bundle; record its calls.

    Returns a dict tallying ``downloads`` and ``uploads`` so a test can assert the
    pipeline fetched the bundle EXACTLY ONCE — the core of the duplicate-pipeline
    fix — rather than the 2x it used to.

    The pipeline STREAMS the bundle to a temp file (it never pulls the whole
    archive into the orchestrator's heap), so the fetch seam patched here is
    ``_download_artifact_bundle_to_path``: the fake writes the in-memory test
    bundle to the destination path the pipeline streams into. Any code path that
    reached for the whole-bundle ``storage.download_compute_job_artifact_bundle``
    instead would BLOW UP here — it is patched to fail — which is what keeps the
    OOM-shaped fetch from creeping back into the viewable pass.
    """
    calls: dict = {"downloads": 0, "uploads": []}

    def _fake_stream_download(_job_id, dest_path):
        calls["downloads"] += 1
        with open(dest_path, "wb") as handle:
            handle.write(bundle)
        return True

    async def _forbid_whole_bundle_download(_job_id):
        raise AssertionError(
            "the viewable pass must STREAM the bundle to disk "
            "(_download_artifact_bundle_to_path), never load it whole into memory"
        )

    async def _fake_upload(job_id, basename, data, content_type):
        if fail_on is not None and fail_on in basename:
            raise RuntimeError("simulated GCS upload error")
        calls["uploads"].append(
            {
                "basename": basename,
                "size": len(data),
                "content_type": content_type,
                "streamed": False,
            }
        )
        return (
            f"gs://test-bucket/compute-jobs/{job_id}/artifacts/viewable/{basename}"
        )

    async def _fake_stream_upload(job_id, basename, fileobj, content_type, *, size=None):
        if fail_on is not None and fail_on in basename:
            raise RuntimeError("simulated GCS stream upload error")
        # Measure the source WITHOUT reading it into memory (seek to end), so the
        # mock itself never reintroduces the OOM the streaming path exists to avoid.
        try:
            fileobj.seek(0, os.SEEK_END)
            measured = fileobj.tell()
            fileobj.seek(0)
        except OSError:
            measured = size
        calls["uploads"].append(
            {
                "basename": basename,
                "size": measured if measured is not None else size,
                "content_type": content_type,
                "streamed": True,
            }
        )
        return (
            f"gs://test-bucket/compute-jobs/{job_id}/artifacts/viewable/{basename}"
        )

    monkeypatch.setattr(
        compute_backends_module,
        "_download_artifact_bundle_to_path",
        _fake_stream_download,
    )
    monkeypatch.setattr(
        storage, "download_compute_job_artifact_bundle", _forbid_whole_bundle_download
    )
    monkeypatch.setattr(storage, "upload_compute_job_viewable_file", _fake_upload)
    monkeypatch.setattr(
        storage, "upload_compute_job_viewable_stream", _fake_stream_upload
    )
    return calls


async def _job_files(db_session, job_id) -> list[ComputeJobFile]:
    return (
        (
            await db_session.execute(
                select(ComputeJobFile).where(ComputeJobFile.compute_job_id == job_id)
            )
        )
        .scalars()
        .all()
    )


# ── GCP VM viewable upload endpoint ──────────────────────────────────────


def _build_viewable_multipart_body(
    file_data: bytes, filename: str
) -> tuple[bytes, str]:
    """Build a ``multipart/form-data`` body matching viewable_watch.py's format."""
    boundary = b"----TestBoundaryViewable"
    parts = [
        b"--" + boundary,
        b'Content-Disposition: form-data; name="file"; filename="'
        + filename.encode("utf-8")
        + b'"',
        b"Content-Type: application/octet-stream",
        b"",
        file_data,
        b"--" + boundary,
        b'Content-Disposition: form-data; name="filename"',
        b"",
        filename.encode("utf-8"),
        b"--" + boundary + b"--",
    ]
    body = b"\r\n".join(parts)
    ct = f'multipart/form-data; boundary={boundary.decode("ascii")}'
    return body, ct


async def _fake_viewable_upload(job_id, basename, data, content_type, *, calls):
    """Async fake for ``storage.upload_compute_job_viewable_file``."""
    calls.append({"basename": basename, "size": len(data)})
    return (
        f"gs://test-bucket/compute-jobs/{job_id}/artifacts/viewable/{basename}"
    )


@pytest.mark.asyncio
async def test_gcp_vm_viewable_upload_large_video_uses_streaming_seam(
    client, db_session, monkeypatch
):
    """A video body above the spool threshold is uploaded via the STREAMING seam.

    The whole point of the spool+streaming path: a multi-hundred-MiB video must
    never be buffered whole in the orchestrator heap.  The test shrinks the spool
    threshold to a trivially small value so the test payload (a few KB) rolls to
    disk and the streaming branch activates, then asserts that
    ``upload_compute_job_viewable_stream`` is called with a file-like object that
    reads back the exact file bytes — while ``upload_compute_job_viewable_file``
    (the OOM vector) is never called.
    """
    from app.routers import agent_compute_jobs as agent_mod

    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    project = Project(name="GCP VM Stream Viewable", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python3 train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.RUNNING
    await db_session.commit()

    token = compute_backends_module.compute_job_heartbeat_secret(job.id)
    video_bytes = b"\x00\x01FAKE-MP4-PAYLOAD" * 64  # ~1 KiB, distinctive

    captured: dict = {}
    fake_uri = (
        f"gs://test-bucket/compute-jobs/{job.id}/artifacts/viewable/rollout.mp4"
    )

    async def _fake_stream(job_id, basename, fileobj, content_type, *, size=None):
        captured["read"] = fileobj.read()
        captured["size"] = size
        captured["content_type"] = content_type
        captured["basename"] = basename
        return fake_uri

    body, content_type = _build_viewable_multipart_body(video_bytes, "rollout.mp4")

    # Shrink the spool threshold so the modest test payload rolls to disk and
    # activates the streaming branch.  The in-memory seam MUST NOT be touched.
    with monkeypatch.context() as mp:
        mp.setattr(agent_mod, "_MAX_VIEWABLE_FILE_BYTES", 8)
        mp.setattr(agent_mod, "_MULTIPART_BODY_MARGIN", 8)
        monkeypatch.setattr(
            storage,
            "upload_compute_job_viewable_stream",
            _fake_stream,
        )
        monkeypatch.setattr(
            storage,
            "upload_compute_job_viewable_file",
            AsyncMock(
                side_effect=AssertionError(
                    "rolled video must not use the in-memory upload seam"
                )
            ),
        )

        resp = await client.post(
            f"/api/agent/compute-jobs/{job.id}/viewable",
            content=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type,
            },
        )

    assert resp.status_code == 200, (
        f"Expected 200 for streamed video, got {resp.status_code}: {resp.text}"
    )
    body_json = resp.json()
    assert body_json["ok"] is True
    assert body_json["entry"]["kind"] == "video"
    assert body_json["entry"]["storage_uri"] == fake_uri
    assert body_json["entry"]["size_bytes"] == len(video_bytes)

    # The streamed bytes are EXACTLY the file part — no multipart framing.
    assert captured["read"] == video_bytes, (
        "streaming seam must yield exactly the file part bytes, no multipart framing"
    )
    assert captured["size"] == len(video_bytes)
    assert captured["content_type"] == "video/mp4"

    # DB row + manifest recorded with the streamed size.
    await db_session.refresh(job)
    vf = (job.polling_metadata or {}).get("viewable_files", [])
    assert len(vf) == 1 and vf[0]["size_bytes"] == len(video_bytes)


@pytest.mark.asyncio
async def test_gcp_vm_heartbeat_stamps_live_viewable_file(
    client, db_session, monkeypatch
):
    """Acceptance test: a GCP VM uploads a viewable file mid-run and it is
    persisted in polling_metadata with ``live=True``, reachable before the
    job reaches a terminal state.

    This is the named acceptance evidence for the 'push mid-run viewables
    from GCP-VM jobs' task.  It exercises the full endpoint seam: auth via
    per-job HMAC secret, multipart body parsing, kind classification,
    per-kind byte caps, GCS upload integration, polling_metadata manifest
    update, and ``ComputeJobFile`` row creation — all without requiring a
    real GCS bucket.
    """
    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    upload_calls: list = []
    monkeypatch.setattr(
        storage,
        "upload_compute_job_viewable_file",
        lambda j, b, d, ct: _fake_viewable_upload(j, b, d, ct, calls=upload_calls),
    )
    monkeypatch.setattr(storage, "delete_compute_job_viewable_file", AsyncMock())
    monkeypatch.setattr(storage, "new_viewable_revision", lambda: "test-rev-001")

    project = Project(name="GCP VM Viewable Acc", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python3 train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.RUNNING
    await db_session.commit()

    token = compute_backends_module.compute_job_heartbeat_secret(job.id)
    png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

    body, content_type = _build_viewable_multipart_body(png_data, "reward.png")

    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/viewable",
        content=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
        },
    )
    assert resp.status_code == 200, (
        f"Expected 200, got {resp.status_code}: {resp.text}"
    )

    data = resp.json()
    assert data["ok"] is True
    assert data["total_viewable_files"] == 1
    entry = data["entry"]
    assert entry["filename"] == "reward.png"
    assert entry["kind"] == "image"
    assert entry["content_type"] == "image/png"
    assert entry["live"] is True
    assert entry["size_bytes"] == len(png_data)

    await db_session.refresh(job)
    meta = job.polling_metadata or {}
    viewable_files = meta.get("viewable_files") or []
    assert len(viewable_files) == 1
    assert viewable_files[0]["filename"] == "reward.png"
    assert viewable_files[0]["live"] is True
    assert viewable_files[0]["kind"] == "image"

    assert len(upload_calls) == 1
    assert upload_calls[0]["basename"] == "reward.png"

    # A ComputeJobFile row was created
    rows = await _job_files(db_session, job.id)
    assert len(rows) == 1
    assert rows[0].safe_relpath == "reward.png"
    assert rows[0].media_type == "image"
    assert rows[0].content_type == "image/png"


@pytest.mark.asyncio
async def test_gcp_vm_viewable_upload_rejects_bad_auth(
    client, db_session, monkeypatch
):
    """POST .../viewable with a wrong bearer token returns 403."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="GCP Viewable Auth", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    job = await compute_jobs.create_compute_job(
        db_session, task, command="true",
        machine_type="e2-standard-2", spend_cap_usd=1.0,
        deadline_seconds=300, backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.RUNNING
    await db_session.commit()
    body, ct = _build_viewable_multipart_body(b"PNG", "test.png")
    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/viewable",
        content=body,
        headers={"Authorization": "Bearer wrong-token", "Content-Type": ct},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_gcp_vm_viewable_upload_rejects_terminal_job(
    client, db_session, monkeypatch
):
    """POST .../viewable for a terminal job returns 409."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    monkeypatch.setattr(storage, "upload_compute_job_viewable_file", AsyncMock())
    monkeypatch.setattr(storage, "delete_compute_job_viewable_file", AsyncMock())
    project = Project(name="GCP Viewable Terminal", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    job = await compute_jobs.create_compute_job(
        db_session, task, command="true",
        machine_type="e2-standard-2", spend_cap_usd=1.0,
        deadline_seconds=300, backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.SUCCEEDED
    await db_session.commit()
    token = compute_backends_module.compute_job_heartbeat_secret(job.id)
    body, ct = _build_viewable_multipart_body(b"PNG", "test.png")
    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/viewable",
        content=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": ct,
        },
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_gcp_vm_viewable_upload_rejects_unsupported_type(
    client, db_session, monkeypatch
):
    """POST .../viewable with an unsupported extension (.html) returns 422."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    monkeypatch.setattr(storage, "upload_compute_job_viewable_file", AsyncMock())
    monkeypatch.setattr(storage, "delete_compute_job_viewable_file", AsyncMock())
    project = Project(name="GCP Viewable Type", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    job = await compute_jobs.create_compute_job(
        db_session, task, command="true",
        machine_type="e2-standard-2", spend_cap_usd=1.0,
        deadline_seconds=300, backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.RUNNING
    await db_session.commit()
    token = compute_backends_module.compute_job_heartbeat_secret(job.id)
    body, ct = _build_viewable_multipart_body(b"<html><body>hi</body></html>", "page.html")
    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/viewable",
        content=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": ct,
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_gcp_vm_viewable_upload_rejects_oversized(
    client, db_session, monkeypatch
):
    """POST .../viewable with a file over the per-kind byte cap returns 413."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    monkeypatch.setattr(storage, "upload_compute_job_viewable_file", AsyncMock())
    monkeypatch.setattr(storage, "delete_compute_job_viewable_file", AsyncMock())
    project = Project(name="GCP Viewable Oversized", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    job = await compute_jobs.create_compute_job(
        db_session, task, command="true",
        machine_type="e2-standard-2", spend_cap_usd=1.0,
        deadline_seconds=300, backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.RUNNING
    await db_session.commit()
    token = compute_backends_module.compute_job_heartbeat_secret(job.id)
    oversized = b"\x89PNG\r\n\x1a\n" + b"\x00" * (9 * 1024 * 1024)
    body, ct = _build_viewable_multipart_body(oversized, "huge.png")
    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/viewable",
        content=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": ct,
        },
    )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_gcp_vm_viewable_upload_cap_enforced(
    client, db_session, monkeypatch
):
    """POST .../viewable when the 25-file cap is reached returns 429."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    monkeypatch.setattr(storage, "delete_compute_job_viewable_file", AsyncMock())
    monkeypatch.setattr(storage, "new_viewable_revision", lambda: "test-rev")

    upload_count = 0
    async def _fake_up(job_id, basename, data, ct):
        nonlocal upload_count
        upload_count += 1
        return f"gs://test-bucket/compute-jobs/{job_id}/artifacts/viewable/{basename}"
    monkeypatch.setattr(storage, "upload_compute_job_viewable_file", _fake_up)

    project = Project(name="GCP Viewable Cap", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    job = await compute_jobs.create_compute_job(
        db_session, task, command="true",
        machine_type="e2-standard-2", spend_cap_usd=1.0,
        deadline_seconds=300, backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.RUNNING
    preload = []
    for i in range(25):
        preload.append({
            "filename": f"file{i:02d}.png",
            "storage_uri": (
                f"gs://test-bucket/compute-jobs/{job.id}"
                f"/artifacts/viewable/file{i:02d}.png"
            ),
            "content_type": "image/png",
            "size_bytes": 100,
            "kind": "image",
            "revision": f"r{i}",
            "live": True,
        })
    job.polling_metadata = {"viewable_files": preload}
    await db_session.commit()

    token = compute_backends_module.compute_job_heartbeat_secret(job.id)
    body, ct = _build_viewable_multipart_body(b"PNG", "new_file.png")
    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/viewable",
        content=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": ct,
        },
    )
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_gcp_vm_viewable_upload_refreshes_existing(
    client, db_session, monkeypatch
):
    """Re-uploading the same relpath updates the existing entry (no cap error)."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    monkeypatch.setattr(storage, "delete_compute_job_viewable_file", AsyncMock())
    monkeypatch.setattr(storage, "new_viewable_revision", lambda: "test-rev")

    upload_count = 0
    async def _fake_up(job_id, basename, data, ct):
        nonlocal upload_count
        upload_count += 1
        return f"gs://test-bucket/compute-jobs/{job_id}/artifacts/viewable/{basename}"
    monkeypatch.setattr(storage, "upload_compute_job_viewable_file", _fake_up)

    project = Project(name="GCP Viewable Refresh", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    job = await compute_jobs.create_compute_job(
        db_session, task, command="true",
        machine_type="e2-standard-2", spend_cap_usd=1.0,
        deadline_seconds=300, backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.RUNNING
    await db_session.commit()

    token = compute_backends_module.compute_job_heartbeat_secret(job.id)
    png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

    body, ct = _build_viewable_multipart_body(png_data, "reward.png")
    resp1 = await client.post(
        f"/api/agent/compute-jobs/{job.id}/viewable",
        content=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": ct,
        },
    )
    assert resp1.status_code == 200
    assert resp1.json()["total_viewable_files"] == 1

    newer_data = b"\x89PNG\r\n\x1a\n" + b"\xff" * 200
    body2, ct2 = _build_viewable_multipart_body(newer_data, "reward.png")
    resp2 = await client.post(
        f"/api/agent/compute-jobs/{job.id}/viewable",
        content=body2,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": ct2,
        },
    )
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["total_viewable_files"] == 1
    assert data2["entry"]["size_bytes"] == len(newer_data)

    await db_session.refresh(job)
    meta = job.polling_metadata or {}
    files = meta.get("viewable_files") or []
    assert len(files) == 1
    assert files[0]["size_bytes"] == len(newer_data)


@pytest.mark.asyncio
async def test_gcp_vm_viewable_upload_with_subdir_relpath(
    client, db_session, monkeypatch
):
    """A file from a subdirectory (arm1/reward.png) is stored with flattened name."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    upload_calls: list = []
    monkeypatch.setattr(
        storage,
        "upload_compute_job_viewable_file",
        lambda j, b, d, ct: _fake_viewable_upload(j, b, d, ct, calls=upload_calls),
    )
    monkeypatch.setattr(storage, "delete_compute_job_viewable_file", AsyncMock())
    monkeypatch.setattr(storage, "new_viewable_revision", lambda: "test-rev")

    project = Project(name="GCP Viewable Subdir", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    job = await compute_jobs.create_compute_job(
        db_session, task, command="true",
        machine_type="e2-standard-2", spend_cap_usd=1.0,
        deadline_seconds=300, backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.RUNNING
    await db_session.commit()

    token = compute_backends_module.compute_job_heartbeat_secret(job.id)
    png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    body, ct = _build_viewable_multipart_body(png_data, "arm1/reward.png")
    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/viewable",
        content=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": ct,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["entry"]["filename"] == "arm1__reward.png"
    assert data["total_viewable_files"] == 1
    assert len(upload_calls) == 1
    assert upload_calls[0]["basename"] == "arm1__reward.png"


@pytest.mark.asyncio
async def test_gcp_vm_viewable_upload_missing_filename_field(
    client, db_session, monkeypatch
):
    """A multipart body without a 'filename' field returns 400."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="GCP Viewable NoFn", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    job = await compute_jobs.create_compute_job(
        db_session, task, command="true",
        machine_type="e2-standard-2", spend_cap_usd=1.0,
        deadline_seconds=300, backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.RUNNING
    await db_session.commit()
    token = compute_backends_module.compute_job_heartbeat_secret(job.id)

    boundary = b"----TestBoundaryNoFn"
    parts = [
        b"--" + boundary,
        b'Content-Disposition: form-data; name="file"; filename="x.png"',
        b"Content-Type: application/octet-stream",
        b"",
        b"PNGDATA",
        b"--" + boundary + b"--",
    ]
    body = b"\r\n".join(parts)
    ct = f'multipart/form-data; boundary={boundary.decode("ascii")}'
    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/viewable",
        content=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": ct,
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_gcp_vm_viewable_upload_missing_file_field(
    client, db_session, monkeypatch
):
    """A multipart body without a 'file' field returns 400."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="GCP Viewable NoFile", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    job = await compute_jobs.create_compute_job(
        db_session, task, command="true",
        machine_type="e2-standard-2", spend_cap_usd=1.0,
        deadline_seconds=300, backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.RUNNING
    await db_session.commit()
    token = compute_backends_module.compute_job_heartbeat_secret(job.id)

    boundary = b"----TestBoundaryNoFile"
    parts = [
        b"--" + boundary,
        b'Content-Disposition: form-data; name="filename"',
        b"",
        b"test.png",
        b"--" + boundary + b"--",
    ]
    body = b"\r\n".join(parts)
    ct = f'multipart/form-data; boundary={boundary.decode("ascii")}'
    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/viewable",
        content=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": ct,
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_gcp_vm_viewable_upload_empty_file(
    client, db_session, monkeypatch
):
    """An empty file body returns 400."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name="GCP Viewable Empty", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    job = await compute_jobs.create_compute_job(
        db_session, task, command="true",
        machine_type="e2-standard-2", spend_cap_usd=1.0,
        deadline_seconds=300, backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.RUNNING
    await db_session.commit()
    token = compute_backends_module.compute_job_heartbeat_secret(job.id)
    body, ct = _build_viewable_multipart_body(b"", "empty.png")
    resp = await client.post(
        f"/api/agent/compute-jobs/{job.id}/viewable",
        content=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": ct,
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_gcp_vm_viewable_upload_nonexistent_job(client, monkeypatch):
    """POST .../viewable for a non-existent job returns 404."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    fake_id = uuid.uuid4()
    token = compute_backends_module.compute_job_heartbeat_secret(fake_id)
    body, ct = _build_viewable_multipart_body(b"PNG", "test.png")
    resp = await client.post(
        f"/api/agent/compute-jobs/{fake_id}/viewable",
        content=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": ct,
        },
    )
    assert resp.status_code == 404


# ── Terminal viewable-pipeline tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_viewable_pipeline_roundtrips_images_and_videos(db_session, monkeypatch):
    """A bundle mixing viewable and non-viewable files — only images and videos are
    persisted, on BOTH surfaces, with correct media_type and content_type."""
    job = await _create_bundle_compute_job(db_session)
    png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    jpg_data = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    mp4_data = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100

    bundle = _make_viewable_tar_bundle(
        {
            "./plot.png": png_data,
            "./photo.jpg": jpg_data,
            "./screen.jpeg": jpg_data,
            "./anim.webp": b"RIFF" + b"\x00" * 100,
            "./icon.gif": b"GIF89a" + b"\x00" * 50,
            # SVG is sanitized server-side and now viewable.
            "./diagram.svg": b"<svg></svg>",
            "./output.mp4": mp4_data,
            "./clip.webm": b"\x1aE\xdf\xa3" + b"\x00" * 100,
            # PDF is a first-class viewable kind (matplotlib savefig('*.pdf')).
            "./report.pdf": b"%PDF-1.4\n" + b"\x00" * 100,
            # Non-viewable — must be skipped.
            "./terminal.json": b'{"status": "SUCCEEDED"}',
            "./command.log": b"all good\n",
            "./script.py": b"print('hello')",
            "./results.txt": b"accuracy: 0.95",
        }
    )
    calls = _mock_viewable_storage(monkeypatch, bundle)

    manifest, dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    # 9 viewable files (SVG now viewable — sanitized before upload), uploaded ONCE each.
    assert len(manifest) == 9
    assert dropped == 0
    assert len(calls["uploads"]) == 9
    assert calls["downloads"] == 1, "the bundle must be downloaded exactly once"

    rows = await _job_files(db_session, job.id)
    assert len(rows) == 9

    by_name = {r.safe_relpath: r for r in rows}
    assert by_name["plot.png"].media_type == "image"
    assert by_name["plot.png"].content_type == "image/png"
    assert by_name["output.mp4"].media_type == "video"
    assert by_name["output.mp4"].content_type == "video/mp4"
    assert by_name["report.pdf"].media_type == "pdf"
    assert by_name["report.pdf"].content_type == "application/pdf"
    # SVG is now persisted as kind=image (script-stripped before upload).
    assert by_name["diagram.svg"].media_type == "image"
    assert by_name["diagram.svg"].content_type == "image/svg+xml"
    assert any(e["filename"] == "diagram.svg" for e in manifest)

    # Non-viewable files must NOT appear.
    for skipped in ("terminal.json", "command.log", "script.py", "results.txt"):
        assert skipped not in by_name

    # THE ANTI-DIVERGENCE INVARIANT: both persisted surfaces are written from the
    # SAME single sanitize+upload pass, so they must agree exactly — same files,
    # same canonical URIs. The old two-pipeline design could not guarantee this.
    assert {r.storage_uri for r in rows} == {e["storage_uri"] for e in manifest}
    assert {r.safe_relpath for r in rows} == {e["filename"] for e in manifest}
    assert all("/artifacts/viewable/" in r.storage_uri for r in rows)

    # The manifest is stamped on the job for the admin UI / strategic context.
    meta = job.polling_metadata or {}
    assert meta.get("viewable_file_count") == 9
    assert len(meta.get("viewable_files") or []) == 9


@pytest.mark.asyncio
async def test_pdf_is_viewable_artifact_kind(db_session, monkeypatch):
    """A .pdf plot persisted as kind='pdf' and an .svg as kind='image' (sanitized)
    — end to end from storage classifier through the persistence pipeline."""
    # 1) The storage classifier maps .pdf to (application/pdf, "pdf") on BOTH
    #    viewable surfaces, and the MIME->kind mapper agrees.
    assert storage.viewable_content_type_for_filename("figure.pdf") == (
        "application/pdf",
        "pdf",
    )
    assert "pdf" in storage._VIEWABLE_CONTENT_TYPES
    assert ".pdf" in storage.VIEWABLE_COMPUTE_JOB_EXTENSIONS
    assert storage._media_kind_for_mime("application/pdf") == "pdf"
    # SVG is now also accepted on both surfaces (sanitized before upload).
    assert storage.svg_is_viewable() is True
    assert storage.viewable_content_type_for_filename("figure.svg") == (
        "image/svg+xml",
        "image",
    )

    # 2) A bundle containing a .pdf (alongside a .svg) yields manifest
    #    entries + ComputeJobFile rows for both.
    job = await _create_bundle_compute_job(db_session)
    bundle = _make_viewable_tar_bundle(
        {
            "./savefig.pdf": b"%PDF-1.5\n" + b"\x00" * 200,
            "./diagram.svg": b"<svg><rect width='10' height='10'/></svg>",
        }
    )
    calls = _mock_viewable_storage(monkeypatch, bundle)

    manifest, dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    assert dropped == 0
    assert len(manifest) == 2
    by_name = {e["filename"]: e for e in manifest}
    pdf_entry = by_name["savefig.pdf"]
    assert pdf_entry["kind"] == "pdf"
    assert pdf_entry["content_type"] == "application/pdf"
    svg_entry = by_name["diagram.svg"]
    assert svg_entry["kind"] == "image"
    assert svg_entry["content_type"] == "image/svg+xml"
    assert len(calls["uploads"]) == 2

    rows = await _job_files(db_session, job.id)
    assert len(rows) == 2
    row_by_name = {r.safe_relpath: r for r in rows}
    assert row_by_name["savefig.pdf"].media_type == "pdf"
    assert row_by_name["diagram.svg"].media_type == "image"
    assert row_by_name["diagram.svg"].content_type == "image/svg+xml"


@pytest.mark.asyncio
async def test_viewable_pipeline_skips_non_viewable_files(db_session, monkeypatch):
    """Files with extensions outside the viewable set are never persisted."""
    job = await _create_bundle_compute_job(db_session)
    bundle = _make_viewable_tar_bundle(
        {
            "./data.json": b'{"x": 1}',
            "./readme.md": b"# Hello",
            "./Makefile": b"all:\n\techo hi",
            "./binary.bin": b"\x00\x01\x02\x03",
            "./model.pt": b"pretrained",
            "./index.html": b"<html></html>",  # .html is not in the viewable set
        }
    )
    calls = _mock_viewable_storage(monkeypatch, bundle)

    manifest, dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    assert manifest == []
    assert dropped == 0
    assert calls["uploads"] == []
    assert await _job_files(db_session, job.id) == []
    # The pass still records that it RAN (count 0) — that stamp is the idempotency
    # marker, so a zero-file job must not re-download on re-entry either.
    assert (job.polling_metadata or {}).get("viewable_file_count") == 0


@pytest.mark.asyncio
async def test_viewable_pipeline_enforces_single_per_file_size_cap(
    db_session, monkeypatch
):
    """Files over the canonical per-file cap are skipped; smaller ones still persist.

    ONE cap policy now governs both surfaces. Previously the two pipelines
    disagreed (8 MiB vs 25 MB), so a 20 MB plot was present under one prefix and
    absent from the other.
    """
    job = await _create_bundle_compute_job(db_session)
    cap = compute_backends_module._MAX_VIEWABLE_FILE_BYTES

    oversized = b"\xff\xd8\xff\xe0" + b"X" * (cap + 1024)
    small = b"\x89PNG\r\n\x1a\n" + b"Y" * 1024
    bundle = _make_viewable_tar_bundle(
        {
            "./huge_photo.jpg": oversized,
            "./tiny_plot.png": small,
        }
    )
    calls = _mock_viewable_storage(monkeypatch, bundle)

    manifest, _dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    assert len(manifest) == 1
    assert manifest[0]["filename"] == "tiny_plot.png"
    assert len(calls["uploads"]) == 1

    rows = await _job_files(db_session, job.id)
    assert len(rows) == 1
    assert rows[0].safe_relpath == "tiny_plot.png"


@pytest.mark.asyncio
async def test_viewable_pipeline_streams_large_video_over_image_cap(
    db_session, monkeypatch
):
    """A video over the 8 MiB in-memory image cap is STREAMED, not dropped.

    Rollout/training videos routinely exceed the per-file in-memory ceiling that
    governs images. Rather than being silently dropped (locked inside the opaque
    bundle), they are streamed member-by-member to storage through the dedicated
    streaming seam (``upload_compute_job_viewable_stream``), while a companion
    image keeps taking the in-memory path.
    """
    job = await _create_bundle_compute_job(db_session)
    image_cap = compute_backends_module._MAX_VIEWABLE_FILE_BYTES
    # Incompressible so the archived member genuinely exceeds the image cap.
    big_video = os.urandom(image_cap + 512 * 1024)  # ~8.5 MiB > image cap
    small_png = b"\x89PNG\r\n\x1a\n" + b"Y" * 1024

    bundle = _make_viewable_tar_bundle(
        {
            "./rollout.mp4": big_video,
            "./frame.png": small_png,
        }
    )
    calls = _mock_viewable_storage(monkeypatch, bundle)

    manifest, dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    assert dropped == 0
    # Both persisted — the big video is NOT dropped for exceeding the image cap.
    by_name = {m["filename"]: m for m in manifest}
    assert set(by_name) == {"rollout.mp4", "frame.png"}
    assert by_name["rollout.mp4"]["size_bytes"] == len(big_video)
    assert by_name["rollout.mp4"]["kind"] == "video"
    assert by_name["rollout.mp4"]["content_type"] == "video/mp4"

    uploads = {u["basename"]: u for u in calls["uploads"]}
    # The video went through the STREAMING seam; the image through the in-memory one.
    assert uploads["rollout.mp4"]["streamed"] is True
    assert uploads["rollout.mp4"]["size"] == len(big_video)
    assert uploads["frame.png"]["streamed"] is False

    rows = await _job_files(db_session, job.id)
    names = {r.safe_relpath for r in rows}
    assert names == {"rollout.mp4", "frame.png"}


@pytest.mark.asyncio
async def test_viewable_pipeline_enforces_video_ceiling(db_session, monkeypatch):
    """A video over the (larger) streamed-video ceiling is still skipped.

    Streaming raises the video ceiling far above the image cap, but it is not
    unbounded: a video above ``_MAX_VIEWABLE_VIDEO_BYTES`` is skipped (stays in
    the bundle) while one within the ceiling is streamed. The ceiling is
    monkeypatched down so the test needn't materialize a half-gigabyte file.
    """
    job = await _create_bundle_compute_job(db_session)
    image_cap = compute_backends_module._MAX_VIEWABLE_FILE_BYTES
    video_ceiling = image_cap + 4 * 1024 * 1024  # 12 MiB, above the image cap
    monkeypatch.setattr(
        compute_backends_module, "_MAX_VIEWABLE_VIDEO_BYTES", video_ceiling
    )

    within = os.urandom(image_cap + 1 * 1024 * 1024)  # ~9 MiB: streamed
    over = os.urandom(video_ceiling + 1 * 1024 * 1024)  # ~13 MiB: skipped
    bundle = _make_viewable_tar_bundle(
        {
            "./ok_clip.mp4": within,
            "./too_big.webm": over,
        }
    )
    calls = _mock_viewable_storage(monkeypatch, bundle)

    manifest, dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    # Only the within-ceiling video is persisted; the oversized one is skipped
    # and reported in the aggregate omission count, matching image-size and
    # count-cap omissions.
    assert [m["filename"] for m in manifest] == ["ok_clip.mp4"]
    assert dropped == 1
    uploads = {u["basename"]: u for u in calls["uploads"]}
    assert set(uploads) == {"ok_clip.mp4"}
    assert uploads["ok_clip.mp4"]["streamed"] is True

    rows = await _job_files(db_session, job.id)
    assert {r.safe_relpath for r in rows} == {"ok_clip.mp4"}


def test_viewable_pipeline_streams_large_video_and_peak_memory_is_bounded(
    tmp_path, monkeypatch
):
    """A large video is uploaded WITHOUT ever holding it whole in RAM.

    The whole point of streaming: a video far larger than the 8 MiB in-memory
    image cap must reach storage while the orchestrator's peak allocation stays
    bounded by the copy/stream buffer, not the video size. An implementation that
    buffered the video (``upload_from_string``) would pay for its full size here
    and trip the ceiling.
    """
    monkeypatch.setattr(
        compute_backends_module, "_MAX_VIEWABLE_VIDEO_BYTES", 128 * 1024 * 1024
    )
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096
    # INCOMPRESSIBLE and well over the 8 MiB image cap (but under the video
    # ceiling): the streamed bytes a buffering impl would hold are the full 40 MiB.
    big_video = os.urandom(40 * 1024 * 1024)
    src = tmp_path / "bundle.tar.gz"
    with tarfile.open(str(src), mode="w:gz", compresslevel=1) as tar:
        for name, payload in (
            ("./rollout.mp4", big_video),
            ("./plot.png", png),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    video_size = len(big_video)
    del big_video
    assert src.stat().st_size > 32 * 1024 * 1024, "the video must dwarf the ceiling"

    def _fake_stream(_job_id, dest_path):
        shutil.copyfile(str(src), dest_path)  # to disk; no Python-level buffer
        return True

    async def _forbid_whole_bundle_download(_job_id):
        raise AssertionError("must stream the bundle to disk, never load it whole")

    uploaded: list = []

    async def _fake_file_upload(job_id, filename, data, content_type):
        uploaded.append({"filename": filename, "streamed": False})
        return storage.compute_job_viewable_uri(job_id, filename)

    async def _fake_stream_upload(job_id, filename, fileobj, content_type, *, size=None):
        # Measure via seek — reading the file whole here would defeat the test.
        fileobj.seek(0, os.SEEK_END)
        measured = fileobj.tell()
        fileobj.seek(0)
        uploaded.append({"filename": filename, "streamed": True, "size": measured})
        return storage.compute_job_viewable_uri(job_id, filename)

    job = MagicMock()
    job.id = uuid.uuid4()
    job.backend = compute_backends_module.GCP_BACKEND_NAME

    with patch.object(
        compute_backends_module, "_download_artifact_bundle_to_path", _fake_stream
    ), patch.object(
        compute_backends_module.storage,
        "download_compute_job_artifact_bundle",
        _forbid_whole_bundle_download,
    ), patch.object(
        compute_backends_module.storage,
        "upload_compute_job_viewable_file",
        _fake_file_upload,
    ), patch.object(
        compute_backends_module.storage,
        "upload_compute_job_viewable_stream",
        _fake_stream_upload,
    ):
        tracemalloc.start()
        try:
            manifest, dropped = asyncio.run(
                compute_backends_module.persist_viewable_artifacts(job)
            )
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

    assert sorted(m["filename"] for m in manifest) == ["plot.png", "rollout.mp4"]
    assert dropped == 0
    by_name = {u["filename"]: u for u in uploaded}
    assert by_name["rollout.mp4"]["streamed"] is True
    assert by_name["rollout.mp4"]["size"] == video_size
    assert by_name["plot.png"]["streamed"] is False

    # Peak stays bounded — well under the 40 MiB video — proving the video is
    # streamed, not materialized in the orchestrator's heap.
    ceiling = 16 * 1024 * 1024
    assert peak < ceiling, (
        f"the viewable pass peaked at {peak / 1024 / 1024:.1f} MiB streaming a "
        f"{video_size / 1024 / 1024:.1f} MiB video — it is buffering the video "
        "instead of streaming it (this is the OOM the task fixes)"
    )


@pytest.mark.asyncio
async def test_viewable_pipeline_enforces_single_file_count_cap(
    db_session, monkeypatch
):
    """At most the canonical N viewable files are persisted; the excess is REPORTED.

    The old duplicate pipeline capped at a different count (50 vs 25) and took the
    first N in arbitrary tar order; the canonical pass sorts deterministically
    before capping and reports how many it dropped.
    """
    job = await _create_bundle_compute_job(db_session)
    cap = compute_backends_module._MAX_VIEWABLE_FILE_COUNT
    overflow = 5

    members = {}
    for i in range(cap + overflow):
        members[f"./plot_{i:03d}.png"] = b"\x89PNG\r\n\x1a\n" + str(i).encode() * 10
    bundle = _make_viewable_tar_bundle(members)
    calls = _mock_viewable_storage(monkeypatch, bundle)

    manifest, dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    assert len(manifest) == cap
    assert dropped == overflow, "the pass must report the files it dropped"
    assert len(calls["uploads"]) == cap
    assert len(await _job_files(db_session, job.id)) == cap

    # Deterministic selection: the sorted-first `cap` filenames, not tar order.
    expected = sorted(f"plot_{i:03d}.png" for i in range(cap + overflow))[:cap]
    assert [e["filename"] for e in manifest] == expected
    assert (job.polling_metadata or {}).get("viewable_files_dropped") == overflow


@pytest.mark.asyncio
async def test_viewable_pipeline_sanitizes_paths_and_keeps_subdir_context(
    db_session, monkeypatch
):
    """Traversal/absolute members are rejected; subdir context is folded, not lost.

    ONE sanitizer now. ``arm1/reward.png`` and ``arm2/reward.png`` are distinct
    objects (``arm1__reward.png`` / ``arm2__reward.png``) instead of colliding, and
    ``..`` / absolute members are refused outright.
    """
    job = await _create_bundle_compute_job(db_session)
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00"
    bundle = _make_viewable_tar_bundle(
        {
            "./safe_plot.png": png,
            "../escape.png": png,
            "./subdir/../../escape2.png": png,
            "/etc/passwd.png": png,  # absolute path rejected
            "./arm1/reward.png": png,
            "./arm2/reward.png": png,
        }
    )
    calls = _mock_viewable_storage(monkeypatch, bundle)

    manifest, _dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    persisted = {e["filename"] for e in manifest}
    assert persisted == {"safe_plot.png", "arm1__reward.png", "arm2__reward.png"}
    assert {u["basename"] for u in calls["uploads"]} == persisted

    # No traversal/absolute member may reach storage under any name.
    for uploaded in calls["uploads"]:
        assert ".." not in uploaded["basename"]
        assert not uploaded["basename"].startswith("/")

    rows = await _job_files(db_session, job.id)
    assert {r.safe_relpath for r in rows} == persisted
    # Same-named plots from different arms stay DISTINCT rows (no collision).
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_viewable_pipeline_upload_failure_is_non_blocking(
    db_session, monkeypatch
):
    """One file's upload failure must not prevent the others being persisted."""
    job = await _create_bundle_compute_job(db_session)
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00"
    bundle = _make_viewable_tar_bundle(
        {
            "./good_a.png": png,
            "./fail_me.png": png,
            "./good_b.png": png,
            "./also_bad_fail_me.jpg": b"\xff\xd8\xff\xe0\x00",
            "./good_c.png": png,
        }
    )
    _calls = _mock_viewable_storage(monkeypatch, bundle, fail_on="fail_me")

    manifest, _dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    # 3 of 5 survived; only durably-stored files appear on either surface.
    assert {e["filename"] for e in manifest} == {
        "good_a.png",
        "good_b.png",
        "good_c.png",
    }
    rows = await _job_files(db_session, job.id)
    assert {r.safe_relpath for r in rows} == {
        "good_a.png",
        "good_b.png",
        "good_c.png",
    }
    # A file that never landed in storage must NOT be advertised as signable.
    assert not any("fail_me" in r.storage_uri for r in rows)
    meta = job.polling_metadata or {}
    assert meta.get("viewable_terminal_reconciliation_incomplete") is True
    assert "viewable_file_count" not in meta


@pytest.mark.asyncio
async def test_terminal_viewable_total_failure_preserves_live_entries_and_retries(
    db_session, monkeypatch
):
    """A corrupt/unavailable terminal bundle must not erase durable live previews.

    The incomplete pass also must not stamp the idempotency marker: a later
    healthy terminal pass retries and authoritatively reconciles the previews.
    """
    job = await _create_bundle_compute_job(db_session)
    live_manifest = [
        {
            "filename": "plot.png",
            "storage_uri": storage.compute_job_viewable_uri(job.id, "plot.png"),
            "content_type": "image/png",
            "size_bytes": 11,
            "kind": "image",
            "revision": "live-plot-revision",
            "live": True,
        },
        {
            "filename": "clip.mp4",
            "storage_uri": storage.compute_job_viewable_uri(job.id, "clip.mp4"),
            "content_type": "video/mp4",
            "size_bytes": 12,
            "kind": "video",
            "revision": "live-clip-revision",
            "live": True,
        },
    ]
    job.polling_metadata = {"viewable_files": live_manifest}
    db_session.add(job)
    await db_session.commit()

    failed_calls = _mock_viewable_storage(monkeypatch, b"not-a-tar")
    preserved, dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    assert dropped == 0
    assert failed_calls["uploads"] == []
    assert {entry["filename"] for entry in preserved} == {"plot.png", "clip.mp4"}
    assert all(entry["live"] is True for entry in preserved)
    failed_meta = job.polling_metadata or {}
    assert failed_meta.get("viewable_terminal_reconciliation_incomplete") is True
    assert "viewable_file_count" not in failed_meta

    healthy_bundle = _make_viewable_tar_bundle(
        {
            "./plot.png": b"\x89PNG terminal",
            "./clip.mp4": b"\x00\x00\x00\x18ftypmp42 terminal",
        }
    )
    healthy_calls = _mock_viewable_storage(monkeypatch, healthy_bundle)
    reconciled, dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    assert dropped == 0
    assert len(healthy_calls["uploads"]) == 2
    assert {entry["filename"] for entry in reconciled} == {"plot.png", "clip.mp4"}
    assert all(entry["live"] is False for entry in reconciled)
    complete_meta = job.polling_metadata or {}
    assert complete_meta.get("viewable_file_count") == 2
    assert "viewable_terminal_reconciliation_incomplete" not in complete_meta


@pytest.mark.asyncio
async def test_terminal_viewable_partial_upload_preserves_unreconciled_live_entry(
    db_session, monkeypatch
):
    """A partial terminal upload replaces successes but retains failed live files."""
    job = await _create_bundle_compute_job(db_session)
    failed_live_revision = "live-failed-file-revision"
    job.polling_metadata = {
        "viewable_files": [
            {
                "filename": "good.png",
                "storage_uri": storage.compute_job_viewable_uri(job.id, "good.png"),
                "content_type": "image/png",
                "size_bytes": 8,
                "kind": "image",
                "revision": "old-good-revision",
                "live": True,
            },
            {
                "filename": "fail_me.png",
                "storage_uri": storage.compute_job_viewable_uri(
                    job.id, "fail_me.png"
                ),
                "content_type": "image/png",
                "size_bytes": 9,
                "kind": "image",
                "revision": failed_live_revision,
                "live": True,
            },
        ]
    }
    db_session.add(job)
    await db_session.commit()

    bundle = _make_viewable_tar_bundle(
        {
            "./good.png": b"\x89PNG good-terminal",
            "./fail_me.png": b"\x89PNG failed-terminal",
        }
    )
    _mock_viewable_storage(monkeypatch, bundle, fail_on="fail_me")

    manifest, dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    assert dropped == 0
    by_name = {entry["filename"]: entry for entry in manifest}
    assert set(by_name) == {"good.png", "fail_me.png"}
    assert by_name["good.png"]["live"] is False
    assert by_name["good.png"]["revision"] != "old-good-revision"
    assert by_name["fail_me.png"]["live"] is True
    assert by_name["fail_me.png"]["revision"] == failed_live_revision
    meta = job.polling_metadata or {}
    assert meta.get("viewable_terminal_reconciliation_incomplete") is True
    assert "viewable_file_count" not in meta


@pytest.mark.asyncio
async def test_viewable_pipeline_is_idempotent_and_downloads_bundle_once(
    db_session, monkeypatch
):
    """Re-entry must not re-download, re-upload, or duplicate rows.

    This is the regression cover for the duplicate-pipeline waste: the SAME terminal
    transition reaches both the call site and ``_link_terminal_artifact``, and the
    bundle must still be fetched exactly ONCE across both.
    """
    job = await _create_bundle_compute_job(db_session)
    png = b"\x89PNG\r\n\x1a\n" + b"Z" * 42
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"W" * 42
    bundle = _make_viewable_tar_bundle({"./chart.png": png, "./anim.mp4": mp4})
    calls = _mock_viewable_storage(monkeypatch, bundle)

    manifest1, _d1 = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()
    assert len(manifest1) == 2
    first_revisions = {e["filename"]: e["revision"] for e in manifest1}
    assert all(e["live"] is False for e in manifest1)
    assert calls["downloads"] == 1
    assert len(calls["uploads"]) == 2

    # Second pass over the same job — the idempotency guard must short-circuit it.
    manifest2, _d2 = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    assert calls["downloads"] == 1, (
        "re-entry must NOT re-download the bundle — this is exactly the waste the "
        "single-pipeline collapse removes"
    )
    assert len(calls["uploads"]) == 2, "re-entry must NOT re-upload any file"
    assert {e["filename"] for e in manifest2} == {e["filename"] for e in manifest1}
    assert {e["filename"]: e["revision"] for e in manifest2} == first_revisions

    rows = await _job_files(db_session, job.id)
    assert len(rows) == 2, f"expected no duplicate rows on re-entry, got {len(rows)}"


@pytest.mark.asyncio
async def test_terminal_extract_purges_orphaned_computejobfile_rows(
    db_session, monkeypatch
):
    """ComputeJobFile rows for mid-run uploads not in the terminal manifest are deleted.

    A file uploaded mid-run but not selected into the terminal top-N manifest leaves
    an orphaned ComputeJobFile row (still signable via the download-URL endpoint) and
    an orphaned GCS object. The terminal extraction pass must delete both.
    """
    job = await _create_bundle_compute_job(db_session)
    orphan_uri = storage.compute_job_viewable_uri(job.id, "orphan_live.png")
    # Pre-populate an orphaned ComputeJobFile row simulating a mid-run upload that
    # didn't make the terminal cut.
    orphan_row = ComputeJobFile(
        compute_job_id=job.id,
        safe_relpath="orphan_live.png",
        media_type="image",
        storage_uri=orphan_uri,
        content_type="image/png",
        file_size_bytes=500,
    )
    db_session.add(orphan_row)
    await db_session.commit()

    deleted_uris: list[str] = []

    async def _fake_delete(uri: str) -> bool:
        deleted_uris.append(uri)
        return True

    monkeypatch.setattr(storage, "delete_storage_uri_best_effort", _fake_delete)

    # Terminal bundle contains only terminal.png — orphan is NOT in the bundle.
    bundle = _make_viewable_tar_bundle(
        {"./terminal.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 50}
    )
    _mock_viewable_storage(monkeypatch, bundle)

    manifest, dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    # The terminal manifest contains only the terminal file.
    assert len(manifest) == 1
    assert manifest[0]["filename"] == "terminal.png"

    # The orphaned GCS object was deleted.
    assert orphan_uri in deleted_uris, (
        "orphaned GCS object must be deleted when not in terminal manifest"
    )

    # The orphaned ComputeJobFile row was removed from the DB.
    rows = await _job_files(db_session, job.id)
    names = {r.safe_relpath for r in rows}
    assert "orphan_live.png" not in names, (
        "orphaned ComputeJobFile row must be purged at terminal extract"
    )
    assert "terminal.png" in names


@pytest.mark.asyncio
async def test_viewable_pipeline_failsoft_on_empty_or_bad_bundle(
    db_session, monkeypatch
):
    """An empty / unparseable bundle yields no files and NEVER raises.

    Viewable persistence must never block a job's terminal completion.
    """
    for blob in (b"", b"not-a-tar", b"\x00" * 100):
        job = await _create_bundle_compute_job(db_session)
        calls = _mock_viewable_storage(monkeypatch, blob)

        manifest, dropped = await compute_jobs.persist_viewable_files_for_job(
            db_session, job
        )
        await db_session.commit()

        assert manifest == []
        assert dropped == 0
        assert calls["uploads"] == []
        assert await _job_files(db_session, job.id) == []


@pytest.mark.asyncio
async def test_viewable_pipeline_skips_backends_without_a_bundle(
    db_session, monkeypatch
):
    """A local-subprocess job has no durable bundle — the pass must not fetch one."""
    job = await _create_bundle_compute_job(db_session, backend="local-subprocess")
    calls = _mock_viewable_storage(monkeypatch, b"")

    manifest, dropped = await compute_jobs.persist_viewable_files_for_job(
        db_session, job
    )
    await db_session.commit()

    assert manifest == []
    assert dropped == 0
    assert calls["downloads"] == 0, "a local job must never download a bundle"


# ── No-progress watchdog ──────────────────────────────────────────────────────
#
# The surfacing half (bounded progress_series + declared_metric) already
# shipped; these cover the CONSUMER wired in this change: _no_progress_stalled
# and its enforcement in _process_single_compute_job.


# Six ages spanning >WINDOW (1800s): oldest is 2000s old (satisfies the "full
# window of history" gate) and five samples fall inside the trailing 1800s
# window (satisfies the min-sample floor).
_SPAN_AGES = [2000, 1500, 1100, 700, 300, 0]


def _progress_job(*, metric, samples, started_offset_seconds, extra_meta=None):
    """Build an in-memory job-like object for pure watchdog-trend tests.

    ``samples`` is a list of ``(seconds_before_now, name, value)`` tuples for the
    progress history; ``started_offset_seconds`` is how long ago the job
    started.
    """
    from types import SimpleNamespace

    now = _utcnow()
    meta = dict(extra_meta or {})
    if metric is not None:
        meta["declared_metric"] = metric
    series = [
        {
            "t": (now - timedelta(seconds=age)).isoformat(),
            "received_at": (now - timedelta(seconds=age)).isoformat(),
            "name": name,
            "value": value,
        }
        for (age, name, value) in samples
    ]
    meta["progress_series"] = series
    return SimpleNamespace(
        polling_metadata=meta,
        started_at=now - timedelta(seconds=started_offset_seconds),
    ), now


def test_no_progress_stalled_flat_metric_over_window_fires():
    metric = "val/loss"
    # A flat metric spanning >1 window, job running ~2h.
    samples = [(age, metric, 2.5000001) for age in _SPAN_AGES]
    job, now = _progress_job(
        metric=metric, samples=samples, started_offset_seconds=7200
    )
    assert compute_jobs._no_progress_stalled(job, now=now) is True


def test_no_progress_stalled_improving_metric_does_not_fire():
    metric = "val/loss"
    # Monotonically decreasing loss across the window → real progress.
    samples = [(age, metric, 3.0 - i * 0.2) for i, age in enumerate(_SPAN_AGES)]
    job, now = _progress_job(
        metric=metric, samples=samples, started_offset_seconds=7200
    )
    assert compute_jobs._no_progress_stalled(job, now=now) is False


def test_no_progress_stalled_requires_min_samples():
    metric = "val/loss"
    # Only 2 flat samples spanning the window — below the min-sample floor.
    samples = [(2000, metric, 1.0), (0, metric, 1.0)]
    job, now = _progress_job(
        metric=metric, samples=samples, started_offset_seconds=7200
    )
    assert compute_jobs._no_progress_stalled(job, now=now) is False


def test_no_progress_stalled_requires_window_of_history():
    metric = "val/loss"
    # Enough flat samples but all bunched into the last ~2 minutes — the metric
    # history does not yet span a full window, so no stall can be declared.
    samples = [(120 - i * 20, metric, 1.0) for i in range(6)]
    job, now = _progress_job(
        metric=metric, samples=samples, started_offset_seconds=7200
    )
    assert compute_jobs._no_progress_stalled(job, now=now) is False


def test_no_progress_stalled_treats_stale_buffer_as_missing_progress():
    metric = "val/loss"
    # A flat buffer whose newest sample predates the trailing window entirely
    # A flat buffer whose newest sample predates the trailing window entirely
    # means a declared watchdog has received no current progress for a full
    # window. That is an enforcement failure and must fire, while backend poll
    # failures remain a separate safety-kill path.
    samples = [(3600 - i * 300, metric, 1.0) for i in range(6)]  # ages 3600..2100
    job, now = _progress_job(
        metric=metric, samples=samples, started_offset_seconds=7200
    )
    assert compute_jobs._no_progress_stalled(job, now=now) is True


def test_no_progress_stalled_respects_grace_period():
    metric = "val/loss"
    samples = [(age, metric, 1.0) for age in _SPAN_AGES]
    # Job only started 10 minutes ago (< 30m grace) — never cut this early even
    # though the (back-dated) samples look flat.
    job, now = _progress_job(
        metric=metric, samples=samples, started_offset_seconds=600
    )
    assert compute_jobs._no_progress_stalled(job, now=now) is False


def test_no_progress_stalled_no_declared_metric_is_fail_open():
    # Flat samples exist, but the job declares no metric → watchdog never fires.
    samples = [(age, "val/loss", 1.0) for age in _SPAN_AGES]
    job, now = _progress_job(
        metric=None, samples=samples, started_offset_seconds=7200
    )
    assert compute_jobs._no_progress_stalled(job, now=now) is False


def test_no_progress_stalled_ignores_other_metric_names():
    metric = "val/loss"
    # The declared metric only has 1 sample; a DIFFERENT metric is flat. The
    # watchdog must key strictly on the declared metric and stay fail-open.
    samples = [(0, metric, 1.0)] + [(age, "throughput", 5.0) for age in _SPAN_AGES]
    job, now = _progress_job(
        metric=metric, samples=samples, started_offset_seconds=7200
    )
    assert compute_jobs._no_progress_stalled(job, now=now) is False


def test_no_progress_stalled_skips_malformed_samples():
    metric = "val/loss"
    job, now = _progress_job(
        metric=metric,
        samples=[(age, metric, 1.0) for age in _SPAN_AGES],
        started_offset_seconds=7200,
    )
    # Inject garbage entries: they must be skipped, not crash or corrupt trend.
    job.polling_metadata["progress_series"].extend(
        [
            "not-a-dict",
            {"name": metric, "value": "not-a-number", "t": "bad"},
            {"name": metric, "value": 1.0},  # missing t
        ]
    )
    assert compute_jobs._no_progress_stalled(job, now=now) is True


def test_no_progress_stalled_disabled_when_window_zero(monkeypatch):
    monkeypatch.setattr(compute_jobs, "COMPUTE_JOB_NO_PROGRESS_WINDOW_SECONDS", 0.0)
    metric = "val/loss"
    samples = [(age, metric, 1.0) for age in _SPAN_AGES]
    job, now = _progress_job(
        metric=metric, samples=samples, started_offset_seconds=7200
    )
    assert compute_jobs._no_progress_stalled(job, now=now) is False


@pytest.mark.asyncio
async def test_create_compute_job_persists_declared_metric(db_session):
    project = Project(name="Declared Metric Persist", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(project_id=project.id, status=TaskStatus.IN_PROGRESS)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        declared_metric="  val/loss  ",
    )
    # Stored trimmed so the watchdog matches the runner-reported sample name.
    assert job.polling_metadata["declared_metric"] == "val/loss"


@pytest.mark.asyncio
async def test_process_compute_jobs_once_cuts_stalled_no_progress_job(
    db_session, monkeypatch
):
    project = Project(name="No Progress Watchdog", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=86400,
        declared_metric="val/loss",
    )
    now = _utcnow()
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    # A long-running (5h) job with a flat declared metric spanning >1 window.
    meta["runtime_deadline_at"] = (now + timedelta(hours=20)).isoformat()
    meta["progress_series"] = [
        {
            "t": (now - timedelta(seconds=age)).isoformat(),
            "received_at": (now - timedelta(seconds=age)).isoformat(),
            "name": "val/loss",
            "value": 2.71828,
        }
        for age in (2000, 1500, 1100, 700, 300, 0)
    ]
    job.polling_metadata = meta
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = "no-progress-job"
    job.started_at = now - timedelta(hours=5)
    await db_session.commit()

    terminate_reasons: list[str] = []

    class FakeBackend:
        def external_id_for(self, persisted_job):
            return "no-progress-job"

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.05, measured_at=as_of)

        async def poll_status(self, persisted_job):
            return ComputeStatusResult(status=ComputeJobStatus.RUNNING)

        async def terminate(self, persisted_job, *, reason):
            terminate_reasons.append(reason)
            return True

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    assert terminate_reasons == ["no_progress"]
    assert job.status == ComputeJobStatus.TIMED_OUT
    assert job.outcome == ComputeJobOutcome.TIMED_OUT
    assert job.polling_metadata["terminal_kind"] == "no_progress"
    assert "no measurable progress" in (job.terminal_reason or "")


@pytest.mark.asyncio
async def test_process_compute_jobs_once_keeps_progressing_job_running(
    db_session, monkeypatch
):
    project = Project(name="Progressing Job", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=86400,
        declared_metric="val/loss",
    )
    now = _utcnow()
    meta = dict(job.polling_metadata or {})
    meta["preflight_smoke_done"] = True
    meta["runtime_deadline_at"] = (now + timedelta(hours=20)).isoformat()
    # Loss is still steadily improving → not a stall.
    meta["progress_series"] = [
        {
            "t": (now - timedelta(seconds=age)).isoformat(),
            "received_at": (now - timedelta(seconds=age)).isoformat(),
            "name": "val/loss",
            "value": 3.0 - i * 0.15,
        }
        for i, age in enumerate((2000, 1500, 1100, 700, 300, 0))
    ]
    job.polling_metadata = meta
    job.status = ComputeJobStatus.RUNNING
    job.external_compute_id = "progressing-job"
    job.started_at = now - timedelta(hours=5)
    await db_session.commit()

    class FakeBackend:
        def external_id_for(self, persisted_job):
            return "progressing-job"

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.05, measured_at=as_of)

        async def poll_status(self, persisted_job):
            return ComputeStatusResult(status=ComputeJobStatus.RUNNING)

        async def terminate(self, persisted_job, *, reason):
            raise AssertionError("progressing job must not be terminated")

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: FakeBackend()
    )
    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job)
    assert job.status == ComputeJobStatus.RUNNING


def test_build_compute_launch_kwargs_forwards_declared_metric():
    from app.orchestrator_runner_dispatch import _build_compute_launch_kwargs

    kwargs = _build_compute_launch_kwargs(
        {"declared_metric": "val/acc"},
        command="python x.py",
        spend_cap_usd=1.0,
        deadline_seconds=60,
    )
    assert kwargs["declared_metric"] == "val/acc"

    nested = _build_compute_launch_kwargs(
        {"compute_experiment": {"declared_metric": "val/acc"}},
        command="python x.py",
        spend_cap_usd=1.0,
        deadline_seconds=60,
    )
    assert nested["declared_metric"] == "val/acc"

    absent = _build_compute_launch_kwargs(
        {}, command="python x.py", spend_cap_usd=1.0, deadline_seconds=60
    )
    assert "declared_metric" not in absent


def test_build_compute_launch_kwargs_preserves_resume_watchdog_pair_flat_and_nested():
    """Both supported payload shapes forward a checkpoint resume guard together."""
    from app.orchestrator_runner_dispatch import _build_compute_launch_kwargs

    predecessor = str(uuid.uuid4())
    base = {"command": "python x.py", "spend_cap_usd": 1.0, "deadline_seconds": 60}
    flat = _build_compute_launch_kwargs(
        {"input_artifact_job_id": predecessor, "no_progress_timeout_seconds": 60},
        **base,
    )
    nested_resume = _build_compute_launch_kwargs(
        {
            "compute_experiment": {
                "input_artifact_job_id": predecessor,
                "no_progress_timeout_seconds": 60,
            }
        },
        **base,
    )

    for kwargs in (flat, nested_resume):
        assert kwargs["input_artifact_job_id"] == predecessor
        assert kwargs["no_progress_timeout_seconds"] == 60


# ── Cross-job checkpoint resume (input_artifact_job_id) ───────────────────────


async def _make_resume_predecessor_job(
    db_session, *, project_id, with_bundle: bool = True
) -> ComputeJob:
    """Insert a terminal runner-subprocess predecessor job.

    When ``with_bundle`` is True the row carries a durable ``result_reference`` so
    ``_compute_job_has_durable_bundle`` treats it as a valid resume source; when
    False it has produced no bundle and must be rejected as a resume source.
    """
    job = ComputeJob(
        project_id=project_id,
        backend=compute_backends_module.RUNNER_SUBPROCESS_BACKEND_NAME,
        machine_type="e2-standard-2",
        status=ComputeJobStatus.SUCCEEDED,
        accrued_cost_usd=0.0,
        completed_at=_utcnow() - timedelta(hours=1),
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    if with_bundle:
        job.result_reference = storage.compute_job_artifact_uri(job.id)
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
    return job


async def _make_pinned_runner_and_task(db_session, project_id):
    """Create a runner in the project and a compute_experiment task pinned to it."""
    from app import auth as auth_mod
    from app.models import LocalRunner

    token = auth_mod.create_runner_token(uuid.uuid4())
    runner = LocalRunner(
        project_id=project_id,
        token_hash=auth_mod.hash_runner_token(token),
        capabilities={"task_types": "all", "has_gpu": False},
    )
    db_session.add(runner)
    await db_session.commit()
    await db_session.refresh(runner)

    task = Task(
        project_id=project_id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "compute_experiment"}],
        context={"runner_target_id": str(runner.id)},
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    return runner, token, task


@pytest.mark.asyncio
async def test_compute_job_resume_hydrates_prior_bundle(client, db_session, monkeypatch):
    """End-to-end cross-job checkpoint resume on the runner-subprocess path.

    (1) create_compute_job validates + persists input_artifact_job_id;
    (2) the claim response surfaces it to the runner;
    (3) the new GET .../input-artifact endpoint streams the predecessor's durable
        bundle so the runner can extract the prior checkpoint into its artifact
        dir (the extracted bundle actually contains the checkpoint);
    (4) the runner's reported resume provenance is recorded in polling_metadata
        and exposed (with input_artifact_job_id) on the serialized response.
    """
    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    project = Project(name="Resume Project", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    predecessor = await _make_resume_predecessor_job(
        db_session, project_id=project.id, with_bundle=True
    )
    _runner, token, task = await _make_pinned_runner_and_task(db_session, project.id)

    # The predecessor's durable bundle carries a real checkpoint plus a stale
    # terminal.json from the prior run (which the runner-side extractor strips).
    bundle_bytes = _make_tar_gz_bundle(
        {
            "checkpoint/model.pt": b"WEIGHTS-EPOCH-7",
            "terminal.json": _terminal_record_bytes(status="SUCCEEDED"),
        }
    )

    async def _fake_download(job_id):
        assert str(job_id) == str(predecessor.id)
        return bundle_bytes

    monkeypatch.setattr(storage, "download_compute_job_artifact_bundle", _fake_download)

    # (1) Launch the successor with the resume source — validated + persisted.
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py --resume",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        no_progress_timeout_seconds=120,
        input_artifact_job_id=str(predecessor.id),
        declared_metric="loss",
    )
    assert job.backend == compute_backends_module.RUNNER_SUBPROCESS_BACKEND_NAME
    assert job.polling_metadata.get("input_artifact_job_id") == str(predecessor.id)

    # Forwarded into the launch spec the backend consumes.
    spec = compute_jobs._build_launch_spec(job)
    assert spec.input_artifact_job_id == str(predecessor.id)

    # Move to STARTING so the runner can claim it (mirrors monitor launch tick).
    job.status = ComputeJobStatus.STARTING
    db_session.add(job)
    await db_session.commit()

    # (2) Claim surfaces the resume source to the runner.
    resp = await client.post(
        "/api/runner/next-compute-job",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == str(job.id)
    assert body["input_artifact_job_id"] == str(predecessor.id)

    # (3) The download endpoint streams the predecessor bundle; the extracted
    # bytes actually contain the prior checkpoint (i.e. it hydrates the run).
    dl = await client.get(
        f"/api/runner/compute-job/{job.id}/input-artifact",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert dl.status_code == 200, dl.text
    assert dl.content == bundle_bytes
    with tarfile.open(fileobj=io.BytesIO(dl.content), mode="r:gz") as tar:
        names = tar.getnames()
        assert "checkpoint/model.pt" in names
        extracted = tar.extractfile("checkpoint/model.pt").read()
    assert extracted == b"WEIGHTS-EPOCH-7"

    # (4) Runner reports honest hydration provenance → recorded + serialized.
    rep = await client.post(
        f"/api/runner/compute-job/{job.id}/report",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "exec_status": "running",
            "resume_hydration": {
                "resumed_from_job_id": str(predecessor.id),
                "ok": True,
                "file_count": 1,
                "bytes": len(b"WEIGHTS-EPOCH-7"),
            },
        },
    )
    assert rep.status_code == 200, rep.text
    await db_session.refresh(job)
    provenance = (job.polling_metadata or {}).get("resume_provenance")
    assert provenance is not None
    assert provenance["ok"] is True
    assert provenance["resumed_from_job_id"] == str(predecessor.id)
    assert provenance["file_count"] == 1

    serialized = schemas.serialize_compute_job_response(job)
    assert serialized["input_artifact_job_id"] == str(predecessor.id)
    assert serialized["resume_provenance"]["ok"] is True


@pytest.mark.asyncio
async def test_compute_job_resume_requires_explicit_watchdog_before_persisting(
    db_session, monkeypatch
):
    """Direct callers cannot create an unguarded checkpoint successor row."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    task = await _make_compute_task(db_session)

    with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc:
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py --resume",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
            input_artifact_job_id=str(uuid.uuid4()),
        )

    assert exc.value.detail["error"] == "input_artifact_watchdog_required"
    jobs = (
        await db_session.execute(select(ComputeJob).where(ComputeJob.task_id == task.id))
    ).scalars().all()
    assert jobs == []


@pytest.mark.asyncio
async def test_compute_job_resume_rejects_unknown_input_artifact_job_id(
    db_session, monkeypatch
):
    """A dangling input_artifact_job_id is rejected at launch (fail-closed) and no
    job row is created — never a silent no-op."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    task = await _make_compute_task(db_session)

    with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc:
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
            no_progress_timeout_seconds=60,
            input_artifact_job_id=str(uuid.uuid4()),
        )
    assert exc.value.detail["error"] == "input_artifact_job_not_found"
    jobs = (
        (await db_session.execute(select(ComputeJob).where(ComputeJob.task_id == task.id)))
        .scalars()
        .all()
    )
    assert jobs == []


@pytest.mark.asyncio
async def test_compute_job_resume_rejects_malformed_input_artifact_job_id(
    db_session, monkeypatch
):
    """A non-UUID input_artifact_job_id is rejected with a clear agent-facing error."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    task = await _make_compute_task(db_session)

    with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc:
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
            no_progress_timeout_seconds=60,
            input_artifact_job_id="not-a-uuid",
        )
    assert exc.value.detail["error"] == "invalid_input_artifact_job_id"


@pytest.mark.asyncio
async def test_compute_job_resume_rejects_cross_project_input_artifact(
    db_session, monkeypatch
):
    """A resume source owned by a DIFFERENT project is refused."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    other_project = Project(name="Other Project", user_id="u")
    db_session.add(other_project)
    await db_session.commit()
    await db_session.refresh(other_project)
    foreign = await _make_resume_predecessor_job(
        db_session, project_id=other_project.id, with_bundle=True
    )

    task = await _make_compute_task(db_session)
    with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc:
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
            no_progress_timeout_seconds=60,
            input_artifact_job_id=str(foreign.id),
        )
    assert exc.value.detail["error"] == "input_artifact_job_cross_project"


@pytest.mark.asyncio
async def test_compute_job_resume_rejects_bundleless_input_artifact(
    db_session, monkeypatch
):
    """A resume source that never produced a durable bundle is refused — resuming
    from nothing would be reproducibility dishonesty."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    task = await _make_compute_task(db_session)
    predecessor = await _make_resume_predecessor_job(
        db_session, project_id=task.project_id, with_bundle=False
    )
    with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc:
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
            no_progress_timeout_seconds=60,
            input_artifact_job_id=str(predecessor.id),
        )
    assert exc.value.detail["error"] == "input_artifact_job_has_no_bundle"


@pytest.mark.asyncio
async def test_compute_job_resume_rejects_failed_ancestor_hydration(db_session, monkeypatch):
    """A bundle from a failed resume hydration is never lineage-eligible."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    task = await _make_compute_task(db_session)
    predecessor = await _make_resume_predecessor_job(
        db_session, project_id=task.project_id, with_bundle=True
    )
    predecessor.polling_metadata = {
        "input_artifact_job_id": str(uuid.uuid4()),
        "resume_provenance": {"ok": False, "reason": "extract_error"},
    }
    db_session.add(predecessor)
    await db_session.commit()

    with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc:
        await compute_jobs.create_compute_job(
            db_session,
            task,
            command="python train.py",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
            input_artifact_job_id=str(predecessor.id),
            declared_metric="loss",
            no_progress_timeout_seconds=120,
        )
    assert exc.value.detail["error"] == "input_artifact_job_resume_provenance_invalid"


@pytest.mark.asyncio
async def test_compute_job_resume_rejects_non_terminal_source(db_session, monkeypatch):
    """A still-RUNNING or POLLING predecessor is refused as a resume source.

    Resuming from a non-terminal job risks rehydrating a partial/mid-checkpoint
    bundle — the design doc flags this as 'infinite chaining of dead runs'.
    """
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    task = await _make_compute_task(db_session)

    for non_terminal_status in (
        ComputeJobStatus.RUNNING,
        ComputeJobStatus.POLLING,
        ComputeJobStatus.QUEUED,
        ComputeJobStatus.STARTING,
    ):
        job = ComputeJob(
            project_id=task.project_id,
            backend=compute_backends_module.RUNNER_SUBPROCESS_BACKEND_NAME,
            machine_type="e2-standard-2",
            status=non_terminal_status,
            accrued_cost_usd=0.0,
            result_reference=storage.compute_job_artifact_uri(uuid.uuid4()),
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        with pytest.raises(compute_jobs.ComputeLaunchPreconditionFailed) as exc:
            await compute_jobs.create_compute_job(
                db_session,
                task,
                command="python train.py",
                machine_type="e2-standard-2",
                spend_cap_usd=1.0,
                deadline_seconds=300,
                no_progress_timeout_seconds=60,
                input_artifact_job_id=str(job.id),
            )
        assert exc.value.detail["error"] == "input_artifact_job_not_terminal", (
            f"expected rejection for status={non_terminal_status}"
        )
        assert exc.value.detail["source_status"] == non_terminal_status.value


@pytest.mark.asyncio
async def test_gcp_startup_script_hydrates_input_artifact_bundle(monkeypatch):
    """When a resume source is set, GcpComputeBackend.launch mints a download
    signed URL and the startup script pulls + extracts the predecessor bundle
    into the artifact dir BEFORE the command runs."""
    backend = GcpComputeBackend()
    predecessor_id = uuid.uuid4()
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="python train.py --resume",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
        input_artifact_job_id=str(predecessor_id),
    )

    download_calls: list = []

    async def fake_upload_url(jid, *, expires_at=None):
        return "https://storage.example.com/upload"

    async def fake_download_url(jid, *, expires_at=None):
        download_calls.append(jid)
        return "https://storage.example.com/predecessor-bundle"

    provision_calls: list[str] = []
    provision_task_ids: list[object] = []

    async def fake_provision(
        vm_id, *, task_id, startup_script, machine_type, provisioning_model,
        accelerator=None, accelerator_count=1,
    ):
        provision_task_ids.append(task_id)
        provision_calls.append(startup_script)
        return True

    monkeypatch.setattr(
        storage, "generate_compute_job_artifact_upload_signed_url", fake_upload_url
    )
    monkeypatch.setattr(
        storage, "generate_compute_job_artifact_download_signed_url", fake_download_url
    )
    monkeypatch.setattr(compute, "provision_ephemeral_vm", fake_provision)
    monkeypatch.setattr(compute, "find_vm_zone", lambda _vm_name: None)
    monkeypatch.setattr(compute, "get_vm_zone", lambda _vm_name: "us-central1-f")

    await backend.launch(spec)

    assert download_calls == [str(predecessor_id)]
    assert provision_task_ids == [None]
    script = provision_calls[0]
    # Downloads the predecessor bundle and extracts it into the staging dir first,
    # then moves it into the artifact dir (so partial extracts never corrupt it).
    assert "https://storage.example.com/predecessor-bundle" in script
    assert "/tmp/blueprint_resume_stage" in script
    assert "tarfile.open" in script
    assert "m.isreg() or m.isdir()" in script
    assert 'tf.extract(m,d,filter="data")' in script
    artifact_downloader = base64.b64decode(
        compute_backends_module._ARTIFACT_DOWNLOAD_B64
    ).decode()
    assert 'tar.extract(member, path=dest_dir, filter="data")' in artifact_downloader
    assert "resume_hydration.ok" in script
    assert "resume_hydration.failed" in script


@pytest.mark.asyncio
async def test_gcp_startup_script_no_resume_block_without_source(monkeypatch):
    """Without a resume source the startup script is unchanged (no resume block)."""
    spec = ComputeLaunchSpec(
        job_id=uuid.uuid4(),
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_at=None,
    )
    script = compute_backends_module._build_gcp_startup_script(
        spec, artifact_upload_url="https://storage.example.com/upload"
    )
    assert "resume_hydration.ok" not in script
    assert "blueprint_resume_bundle.tar.gz" not in script


@pytest.mark.asyncio
async def test_compute_job_input_artifact_endpoint_404_without_source(
    client, db_session, monkeypatch
):
    """The input-artifact endpoint 404s when the pinned job declares no resume
    source (fail-closed, no bytes leaked)."""
    monkeypatch.setattr(compute, "LOCAL_MODE", True)

    project = Project(name="No Resume", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _runner, token, task = await _make_pinned_runner_and_task(db_session, project.id)
    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="python train.py",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
    )
    job.status = ComputeJobStatus.STARTING
    db_session.add(job)
    await db_session.commit()

    dl = await client.get(
        f"/api/runner/compute-job/{job.id}/input-artifact",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert dl.status_code == 404


# ===========================================================================
# Release-audit fixes: stale-snapshot VM kill, DOA retry deadline,
# whole-bundle viewable download, unreachable watchdog SIGKILL.
# ===========================================================================


@pytest.mark.asyncio
async def test_monitor_rereads_task_before_terminal_kill_so_midtick_restore_is_respected(
    db_session, monkeypatch
):
    """A task restored DURING the tick must not have its live VM killed.

    The monitor batch-selects up to 50 jobs and then processes them one at a
    time, each doing backend RPCs — so a job late in the batch is reached long
    after its row (and its task's row) was read. The task-terminal VM kill is
    irreversible, so it must be decided on the AUTHORITATIVE task row, never on
    the tick-start snapshot.

    The mid-tick change here is real, driven through the real tick: job A's poll
    (which runs while the tick is in flight) restores job B's task from FAILED
    back to RUNNING — exactly the operator "PATCH a falsely-FAILED task back to
    QUEUED" move. B's VM must survive: no terminate, no CANCELLED.
    """
    project = Project(name=f"midtick-{uuid.uuid4().hex[:8]}", user_id="test")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task_a = Task(
        project_id=project.id,
        type="compute_experiment",
        prompt="a",
        status=TaskStatus.IN_PROGRESS,
    )
    task_b = Task(
        project_id=project.id,
        type="compute_experiment",
        prompt="b",
        status=TaskStatus.IN_PROGRESS,  # flipped to FAILED below, once its job exists
    )
    db_session.add_all([task_a, task_b])
    await db_session.commit()
    await db_session.refresh(task_a)
    await db_session.refresh(task_b)

    async def _running_job(task, external_id):
        job = await compute_jobs.create_compute_job(
            db_session,
            task,
            command="echo ok",
            machine_type="e2-standard-2",
            spend_cap_usd=1.0,
            deadline_seconds=300,
        )
        job.status = ComputeJobStatus.RUNNING
        job.external_compute_id = external_id
        meta = dict(job.polling_metadata or {})
        meta["preflight_smoke_done"] = True
        job.polling_metadata = meta
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)
        return job

    job_a = await _running_job(task_a, "vm-a")
    job_b = await _running_job(task_b, "vm-b")
    # Pin the batch order (the tick sorts by created_at, id): A is processed
    # first, so its poll is what lands the mid-tick change on B's task.
    job_a.created_at = _utcnow() - timedelta(minutes=5)
    job_b.created_at = _utcnow() - timedelta(minutes=1)
    # B's task is terminal as the tick STARTS — this is what the tick's snapshot
    # sees, and what the (unfixed) monitor would kill B's VM on.
    task_b.status = TaskStatus.FAILED
    db_session.add_all([job_a, job_b, task_b])
    await db_session.commit()
    assert job_a.created_at < job_b.created_at

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    terminated: list = []

    class MidTickBackend:
        def external_id_for(self, persisted_job):
            return persisted_job.external_compute_id or "vm-x"

        async def launch(self, spec):  # pragma: no cover - both jobs are RUNNING
            raise AssertionError("launch must not be called")

        async def poll_cost(self, persisted_job, *, as_of):
            return ComputeCostResult(total_cost_usd=0.0, measured_at=as_of)

        async def poll_status(self, persisted_job):
            if persisted_job.external_compute_id == "vm-a":
                # THE MID-TICK CHANGE: task B is restored while the tick is in
                # flight — after the tick's batch snapshot was taken.
                async with session_factory() as session:
                    live_b = await session.get(Task, task_b.id)
                    live_b.status = TaskStatus.IN_PROGRESS
                    session.add(live_b)
                    await session.commit()
            return ComputeStatusResult(status=ComputeJobStatus.RUNNING)

        async def terminate(self, persisted_job, *, reason):
            terminated.append((persisted_job.external_compute_id or "", reason))
            return True

    monkeypatch.setattr(
        compute_jobs, "get_compute_backend", lambda _name: MidTickBackend()
    )

    await compute_jobs.process_compute_jobs_once(session_factory=session_factory)

    await db_session.refresh(job_b)
    await db_session.refresh(task_b)
    assert task_b.status == TaskStatus.IN_PROGRESS  # the restore landed mid-tick
    assert terminated == [], (
        "job B's VM was killed on the STALE batch snapshot — the task-terminal "
        f"kill must re-read the authoritative task row (terminate calls: {terminated})"
    )
    assert job_b.status == ComputeJobStatus.RUNNING
    assert job_b.outcome != ComputeJobOutcome.CANCELLED


@pytest.mark.asyncio
async def test_provisioning_retry_deadline_starts_at_retry_time_not_stale_tick_start(
    db_session,
    monkeypatch,
):
    """Retry 1 must not be dead on arrival after a slow launch.

    The caller reaches the retry only AFTER waiting out the launch leg (an LRO
    that can burn the entire provisioning window) plus a terminate RPC — so the
    tick-start ``now`` is long stale by then. Deriving the new provisioning
    deadline from it wrote a deadline that was already in the PAST: the next tick
    computed ``_provisioning_timeout_seconds() == 0``, re-timed-out the job
    WITHOUT ever attempting a fresh launch, and the whole retry budget evaporated
    in seconds.

    The persisted deadline must be in the future by ~the backoff, measured from
    the real time at retry.
    """
    # This test provisions a `local-subprocess` backend, which resolve_backend()
    # rejects unless LOCAL_MODE is on (compute_backends.py: "local-subprocess
    # backend is unavailable outside LOCAL_MODE"). test-backend.sh sets
    # LOCAL_MODE=true so it passed there; CI leaves it false, so the retry path
    # raised ValueError and the test failed ONLY on CI. Set it explicitly (the
    # established pattern in this file) so the test is env-independent.
    monkeypatch.setattr(compute, "LOCAL_MODE", True)
    project = Project(name=f"doa-{uuid.uuid4().hex[:8]}", user_id="test")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    task = Task(
        project_id=project.id,
        type="compute_experiment",
        prompt="p",
        status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = await compute_jobs.create_compute_job(
        db_session,
        task,
        command="echo ok",
        machine_type="e2-standard-2",
        spend_cap_usd=1.0,
        deadline_seconds=300,
        backend_name="local-subprocess",
    )
    job.status = ComputeJobStatus.STARTING
    db_session.add(job)
    await db_session.commit()

    session_factory = async_sessionmaker(
        resolve_async_engine(db_session),
        expire_on_commit=False,
        class_=AsyncSession,
    )

    before = _utcnow()
    assert await compute_jobs._maybe_retry_provisioning_timeout(session_factory, job.id)
    after = _utcnow()

    await db_session.refresh(job)
    meta = job.polling_metadata or {}
    assert meta.get("provisioning_retry_count") == 1
    deadline = compute_jobs._parse_naive_iso_datetime(
        meta.get("provisioning_deadline_at")
    )
    assert deadline is not None

    # 1. The deadline is in the FUTURE — the next tick gets a real launch window.
    assert deadline > after, (
        f"retry deadline {deadline.isoformat()} is not in the future "
        f"(now={after.isoformat()}) — retry 1 is dead on arrival"
    )
    assert compute_jobs._provisioning_timeout_seconds(job, now=after) > 0.0, (
        "the next tick sees provisioning_timeout_seconds == 0 and re-times-out "
        "the job WITHOUT ever attempting a fresh launch"
    )
    # 2. It is the backoff measured from the retry itself — the deadline sits
    #    within [now, now + backoff(+jitter)], so a caller whose tick-start clock
    #    is minutes old (a slow launch LRO) cannot shrink it, let alone push it
    #    into the past.
    backoff = compute_jobs.exponential_backoff(
        1,
        base=compute_jobs.PROVISIONING_RETRY_BACKOFF_BASE_SECONDS,
        cap=600.0,
    )
    max_backoff = backoff / 0.8 * 1.2  # undo the jitter band both ways
    # The deadline is stamped at retry time (retry_at), which lies somewhere in
    # [before, after] — so `deadline - before` is at most the backoff PLUS however
    # long the retry call actually took (its DB round-trips). Bound the tolerance
    # by the MEASURED call window rather than a fixed fudge: a loaded CI shard can
    # make the DB work take several seconds, and a hardcoded `+5` turned that real
    # timing into a flaky failure (the test passed in isolation, failed under load).
    call_window = (after - before).total_seconds()
    assert (deadline - before).total_seconds() <= max_backoff + call_window + 1


def test_viewable_pipeline_streams_bundle_and_peak_memory_is_bounded(tmp_path):
    """The viewable pass must never hold a job's whole artifact bundle in RAM.

    This is the OOM, as a measurement. A results bundle is unbounded (checkpoints,
    videos, logs), and the unified viewable pass pulled the ENTIRE object into the
    orchestrator (``blob.download_as_bytes()``) — one fat bundle OOMs the API
    process. It must stream to disk and read only the (count-capped) viewable
    members.

    Both transports are modelled HONESTLY, which is what makes the assertion mean
    something:

    * the streaming seam (``_download_artifact_bundle_to_path``) copies the object
      to its destination file — nothing passes through Python memory;
    * the whole-bundle seam (``storage.download_compute_job_artifact_bundle``) does
      what it really does — materialize the object as ``bytes``. That allocation
      lands inside the measurement window, so an implementation that reaches for it
      is caught paying for it.
    """
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096
    # INCOMPRESSIBLE, like a real checkpoint/video: the wire bytes a whole-bundle
    # download would have to hold are the full 48 MiB, not a gzip artefact of it.
    big_payload = os.urandom(48 * 1024 * 1024)
    src = tmp_path / "bundle.tar.gz"
    # Written straight to disk (compresslevel=1) so the TEST never holds the
    # archive either — only the pipeline's own allocations land in the window.
    with tarfile.open(str(src), mode="w:gz", compresslevel=1) as tar:
        for name, payload in (
            ("./checkpoint/model.pt", big_payload),
            ("./plot.png", png),
            ("./arm1/reward.png", png + b"-a1"),
            ("./notes.txt", b"not viewable"),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    del big_payload
    bundle_size = src.stat().st_size
    assert bundle_size > 32 * 1024 * 1024, (
        f"the bundle ({bundle_size} bytes) must dwarf the memory ceiling"
    )

    def _fake_stream(_job_id, dest_path):
        shutil.copyfile(str(src), dest_path)  # to disk; no Python-level buffer
        return True

    async def _fake_whole_bundle_download(_job_id):
        return src.read_bytes()  # a real whole-object fetch: 48+ MiB in memory

    uploaded: list = []

    async def _fake_upload(job_id, filename, data, content_type):
        uploaded.append(filename)
        return storage.compute_job_viewable_uri(job_id, filename)

    job = MagicMock()
    job.id = uuid.uuid4()
    job.backend = compute_backends_module.GCP_BACKEND_NAME

    with patch.object(
        compute_backends_module, "_download_artifact_bundle_to_path", _fake_stream
    ), patch.object(
        compute_backends_module.storage,
        "download_compute_job_artifact_bundle",
        _fake_whole_bundle_download,
    ), patch.object(
        compute_backends_module.storage,
        "upload_compute_job_viewable_file",
        _fake_upload,
    ):
        tracemalloc.start()
        try:
            manifest, dropped = asyncio.run(
                compute_backends_module.persist_viewable_artifacts(job)
            )
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

    # The pass still WORKS: both viewable files are found inside the fat bundle,
    # under their canonical (subdir-preserving) names; non-viewable members skipped.
    assert sorted(m["filename"] for m in manifest) == ["arm1__reward.png", "plot.png"]
    assert dropped == 0
    assert sorted(uploaded) == ["arm1__reward.png", "plot.png"]

    # And it did not pay for the bundle's size in RAM. The ceiling is generous
    # (well under half the archive), so this pins the SHAPE — bounded, not
    # proportional — rather than a brittle exact number.
    ceiling = 16 * 1024 * 1024
    assert peak < ceiling, (
        f"the viewable pass peaked at {peak / 1024 / 1024:.1f} MiB on a "
        f"{bundle_size / 1024 / 1024:.1f} MiB bundle — it is pulling the whole "
        "bundle into the orchestrator instead of streaming it (this is the OOM)"
    )


def test_checkpoint_resume_streams_predecessor_bundle_and_peak_memory_is_bounded(
    tmp_path,
):
    """Cross-job checkpoint resume must never hold the predecessor bundle in RAM.

    The resume path rehydrates a predecessor compute job's DURABLE artifact bundle
    into the new job's artifact dir before the command runs. A results bundle is
    unbounded (multi-GB RL checkpoints), so pulling the whole object into the
    orchestrator (``download_compute_job_artifact_bundle`` -> ``download_as_bytes``
    + ``io.BytesIO``) is the same leader-OOM class the viewable pass already fixed.
    It must stream the bundle to a temp file and extract from disk.

    Both transports are modelled HONESTLY so the assertion means something:

    * the streaming seam (``_download_artifact_bundle_to_path``) copies the object
      to its destination file — nothing passes through Python memory;
    * the whole-bundle seam (``storage.download_compute_job_artifact_bundle``) does
      what it really does — materialize the object as ``bytes``. That allocation
      lands inside the measurement window, so an implementation that reaches for it
      to resume is caught paying for it.
    """
    # INCOMPRESSIBLE, like a real checkpoint: the wire bytes a whole-object
    # download would hold are the full 48 MiB, not a gzip artefact of it.
    big_checkpoint = os.urandom(48 * 1024 * 1024)
    src = tmp_path / "bundle.tar.gz"
    # Written straight to disk (compresslevel=1) so the TEST never holds the
    # archive either — only the resume path's own allocations land in the window.
    with tarfile.open(str(src), mode="w:gz", compresslevel=1) as tar:
        for name, payload in (
            ("./checkpoint/model.pt", big_checkpoint),
            ("./config.json", b'{"lr": 0.001}'),
            ("./logs/train.log", b"step 1000 loss 0.5\n"),
        ):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    del big_checkpoint
    bundle_size = src.stat().st_size
    assert bundle_size > 32 * 1024 * 1024, (
        f"the bundle ({bundle_size} bytes) must dwarf the memory ceiling"
    )

    def _fake_stream(_job_id, dest_path):
        shutil.copyfile(str(src), dest_path)  # to disk; no Python-level buffer
        return True

    async def _fake_whole_bundle_download(_job_id):
        return src.read_bytes()  # a real whole-object fetch: 48+ MiB in memory

    dest_dir = tmp_path / "artifacts"

    with patch.object(
        compute_backends_module, "_download_artifact_bundle_to_path", _fake_stream
    ), patch.object(
        compute_backends_module.storage,
        "download_compute_job_artifact_bundle",
        _fake_whole_bundle_download,
    ):
        tracemalloc.start()
        try:
            extracted = asyncio.run(
                compute_backends_module._rehydrate_input_artifact_bundle(
                    "predecessor-job-id", str(dest_dir)
                )
            )
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

    # The resume still WORKS: every member is rehydrated onto disk under the
    # artifact dir, checkpoint included.
    assert extracted == 3
    assert (dest_dir / "checkpoint" / "model.pt").stat().st_size == 48 * 1024 * 1024
    assert (dest_dir / "config.json").read_bytes() == b'{"lr": 0.001}'
    assert (dest_dir / "logs" / "train.log").exists()

    # And it did not pay for the bundle's size in RAM. The ceiling is generous
    # (well under half the archive), pinning the SHAPE — bounded, not proportional.
    ceiling = 16 * 1024 * 1024
    assert peak < ceiling, (
        f"checkpoint resume peaked at {peak / 1024 / 1024:.1f} MiB on a "
        f"{bundle_size / 1024 / 1024:.1f} MiB bundle — it is buffering the whole "
        "predecessor bundle in the orchestrator heap instead of streaming it to "
        "disk (this is the leader OOM on multi-GB checkpoints)"
    )


@pytest.mark.parametrize("directory_only", [False, True])
def test_local_checkpoint_resume_rejects_bundle_without_regular_files(
    tmp_path, directory_only
):
    """Empty and directory-only archives cannot earn successful provenance."""
    source = tmp_path / "source.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        if directory_only:
            directory = tarfile.TarInfo("checkpoint/")
            directory.type = tarfile.DIRTYPE
            archive.addfile(directory)

    def _fake_stream(_job_id, dest_path):
        shutil.copyfile(source, dest_path)
        return True

    dest_dir = tmp_path / "artifacts"
    with patch.object(
        compute_backends_module, "_download_artifact_bundle_to_path", _fake_stream
    ), pytest.raises(RuntimeError, match="contains no resumable files"):
        asyncio.run(
            compute_backends_module._rehydrate_input_artifact_bundle(
                "predecessor-job-id", str(dest_dir)
            )
        )

    assert not dest_dir.exists()


def test_local_checkpoint_resume_strips_stale_status_files_before_counting(tmp_path):
    """A bundle containing only predecessor status files is not a checkpoint."""
    source = tmp_path / "source.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        for name in compute_backends_module._RESUME_STALE_ARTIFACT_FILENAMES:
            payload = b"stale predecessor status"
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    def _fake_stream(_job_id, dest_path):
        shutil.copyfile(source, dest_path)
        return True

    dest_dir = tmp_path / "artifacts"
    with patch.object(
        compute_backends_module, "_download_artifact_bundle_to_path", _fake_stream
    ), pytest.raises(RuntimeError, match="contains no resumable files"):
        asyncio.run(
            compute_backends_module._rehydrate_input_artifact_bundle(
                "predecessor-job-id", str(dest_dir)
            )
        )

    assert not dest_dir.exists()


def test_local_checkpoint_resume_cleans_injected_partial_atomic_publication(
    tmp_path,
):
    """A publication error after one entry cannot expose a partial durable tree."""
    source = tmp_path / "source.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        for name, payload in (("first.ckpt", b"one"), ("second.ckpt", b"two")):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    def _fake_stream(_job_id, dest_path):
        shutil.copyfile(source, dest_path)
        return True

    dest_dir = tmp_path / "artifacts"
    dest_dir.mkdir()
    (dest_dir / "incomplete-from-prior-attempt.ckpt").write_bytes(b"old")
    real_replace = os.replace

    def _interrupt_atomic_replace(src, dst):
        if os.path.basename(src).startswith(".bp-resume-stage-"):
            os.mkdir(dst)
            first_entry = sorted(os.listdir(src))[0]
            real_replace(os.path.join(src, first_entry), os.path.join(dst, first_entry))
            raise OSError("injected interruption after first top-level entry")
        return real_replace(src, dst)

    with patch.object(
        compute_backends_module, "_download_artifact_bundle_to_path", _fake_stream
    ), patch.object(
        compute_backends_module.os, "replace", _interrupt_atomic_replace
    ), pytest.raises(RuntimeError, match="could not be published"):
        asyncio.run(
            compute_backends_module._rehydrate_input_artifact_bundle(
                "predecessor-job-id", str(dest_dir)
            )
        )

    assert not dest_dir.exists()
    assert not list(tmp_path.glob("artifacts.bp-incomplete-*"))


def test_no_progress_watchdog_escalates_to_sigkill_when_sigterm_is_ignored(
    tmp_path, monkeypatch
):
    """A command that ignores SIGTERM must still be reaped by the no-progress watchdog.

    The watchdog is a background subshell of the job's process-group leader, so the
    ``kill -TERM -$$`` it fires at the group lands on the WATCHDOG ITSELF. It died
    right there — and the SIGKILL escalation two lines below never ran. A command
    that traps/ignores SIGTERM therefore kept running (and kept billing) after the
    watchdog had already written the ``no_progress_timeout`` terminal record: the
    no-progress kill silently did not kill.

    Runs the REAL emitted wrapper against a REAL SIGTERM-ignoring process; only a
    reachable SIGKILL escalation can pass.
    """
    import subprocess
    import sys
    import time

    if not sys.platform.startswith("linux"):
        pytest.skip("process-group signalling + `stat -c` target the Linux substrate")

    monkeypatch.setattr(
        compute_backends_module, "_LOCAL_STATE_ROOT", str(tmp_path / "state")
    )
    external_id = "np-sigkill-e2e"
    pid_file = tmp_path / "stubborn.pid"
    stubborn = tmp_path / "stubborn.py"
    stubborn.write_text(
        "import os, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "with open(sys.argv[1], 'w') as fh:\n"
        "    fh.write(str(os.getpid()))\n"
        "    fh.flush()\n"
        "time.sleep(600)\n",
        encoding="utf-8",
    )

    wrapper = compute_backends_module._build_local_wrapper_command(
        f"python3 {shlex.quote(str(stubborn))} {shlex.quote(str(pid_file))}",
        external_id=external_id,
        no_progress_timeout_seconds=5,  # the command never touches the progress file
    )

    def _alive(pid: int) -> bool:
        """True while the process exists and is not an unreaped zombie."""
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                state = fh.read().rsplit(")", 1)[1].split()[0]
        except OSError:
            return False
        return state != "Z"

    proc = subprocess.Popen(
        ["bash", "-lc", wrapper],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # exactly how the backend spawns it: $$ == the pgid
    )
    stubborn_pid = None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            raw_pid = (
                pid_file.read_text(encoding="utf-8").strip()
                if pid_file.exists()
                else ""
            )
            if raw_pid:
                stubborn_pid = int(raw_pid)
                break
            time.sleep(0.1)
        assert stubborn_pid, "the SIGTERM-ignoring command never started"

        # The watchdog fires at ~5s (no progress), TERMs the group (which the
        # command ignores), and must escalate to SIGKILL ~2s later.
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and _alive(stubborn_pid):
            time.sleep(0.2)

        assert not _alive(stubborn_pid), (
            "the SIGTERM-ignoring command SURVIVED the no-progress watchdog — its "
            "SIGKILL escalation is unreachable (the watchdog killed itself with the "
            "group SIGTERM it sent), so a stalled job keeps running, and keeps "
            "billing, after being reported dead"
        )

        state_dir = compute_backends_module._local_state_dir_for_external_id(external_id)
        terminal_path = compute_backends_module._local_terminal_path_for_external_id(
            external_id
        )
        assert os.path.exists(terminal_path), f"no terminal record in {state_dir}"
        raw = open(terminal_path, encoding="utf-8").read()
        assert "REASON=no_progress_timeout" in raw, raw
        assert "EXIT_CODE=125" in raw, raw
    finally:
        if stubborn_pid and _alive(stubborn_pid):
            try:
                os.kill(stubborn_pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass


def test_no_progress_watchdog_shields_itself_from_its_own_group_term():
    """Shape guard pairing with the e2e test above: the watchdog must ignore TERM
    before it group-TERMs, or its SIGKILL escalation is dead code."""
    wrapper = compute_backends_module._build_local_wrapper_command(
        "echo ok", external_id="np-shape", no_progress_timeout_seconds=120
    )
    lines = [line.strip() for line in wrapper.splitlines()]
    term_line = "kill -TERM -$$ 2>/dev/null || true"
    kill_line = "kill -KILL -$$ 2>/dev/null || true"
    assert "trap '' TERM" in lines, (
        "the watchdog subshell shares the process group it signals — without "
        "ignoring TERM first it kills itself and never reaches the SIGKILL escalation"
    )
    assert lines.index("trap '' TERM") < lines.index(term_line)
    assert lines.index(term_line) < lines.index(kill_line)


def test_local_container_watchdog_names_and_docker_kills_the_container(monkeypatch):
    """Shape guard: a CONTAINERIZED job's watchdog must docker-kill the container.

    The trainer inside runs as a child of dockerd, NOT a member of the wrapper's
    process group, so the watchdog's group SIGKILL can never reach it. The emitted
    wrapper must therefore (a) give the container a deterministic ``--name`` and
    (b) ``docker kill`` THAT name in the watchdog escalation, and do so BEFORE the
    group KILL reaps the watchdog (otherwise the escalation is dead code). A bare
    (non-container) job must NOT gain a docker kill — the group kill reaches it.
    """
    monkeypatch.setattr(compute_backends_module, "detect_local_gpu", lambda: None)
    external_id = "np-container-shape"
    name = compute_backends_module._local_container_name_for_external_id(external_id)
    wrapper = compute_backends_module._build_local_wrapper_command(
        "python train.py",
        external_id=external_id,
        container_image="nvcr.io/nvidia/pytorch:24.01-py3",
        no_progress_timeout_seconds=30,
    )
    assert f"--name {shlex.quote(name)}" in wrapper
    lines = [line.strip() for line in wrapper.splitlines()]
    docker_kill_line = f"docker kill {shlex.quote(name)} 2>/dev/null || true"
    kill_group_line = "kill -KILL -$$ 2>/dev/null || true"
    assert docker_kill_line in lines, (
        "the containerized watchdog must docker kill the container by name — a "
        "group SIGKILL cannot reach the dockerd-parented trainer"
    )
    assert lines.index(docker_kill_line) < lines.index(kill_group_line), (
        "docker kill must run BEFORE the group KILL reaps the watchdog, else the "
        "container-reaching escalation never executes"
    )
    # Bare job: no container, no docker kill (group kill reaches everything).
    bare = compute_backends_module._build_local_wrapper_command(
        "echo ok", external_id="np-bare", no_progress_timeout_seconds=30
    )
    assert "docker kill" not in bare
    assert "--name" not in bare


def test_local_container_watchdog_actually_kills_containerized_workload(
    tmp_path, monkeypatch
):
    """END-TO-END: a TERM-ignoring CONTAINERIZED trainer must be reaped by the
    no-progress watchdog.

    A containerized trainer runs as a child of dockerd, NOT a member of the
    wrapper's process group, so the watchdog's group SIGKILL cannot touch it — a
    TERM-ignoring trainer kept running (and kept billing) after the job was
    recorded FAILED. Only reaching the container directly (``docker kill <name>``)
    stops it.

    Modelled HONESTLY with a fake ``docker`` on PATH that reparents the trainer
    OUT of the wrapper's process group (``start_new_session``), exactly as dockerd
    does: the group signals then genuinely cannot reach the trainer, and ONLY a
    ``docker kill`` targeting the recorded container can. If the wrapper does not
    docker-kill the container, the trainer survives and this test fails.
    """
    import subprocess
    import textwrap
    import time

    if not sys.platform.startswith("linux"):
        pytest.skip("process-group signalling + `/proc` state target the Linux substrate")

    monkeypatch.setattr(compute_backends_module, "detect_local_gpu", lambda: None)
    monkeypatch.setattr(
        compute_backends_module, "_LOCAL_STATE_ROOT", str(tmp_path / "state")
    )
    external_id = "np-container-e2e"
    container_name = compute_backends_module._local_container_name_for_external_id(
        external_id
    )

    # Fake-docker state dir: name -> container (session) pid, so `docker kill
    # <name>` can reach the reparented trainer.
    fake_state = tmp_path / "fakedocker"
    fake_state.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(
            f"""
            import os, sys, signal, subprocess
            STATE = {str(fake_state)!r}
            argv = sys.argv[1:]
            if not argv:
                sys.exit(0)
            verb = argv[0]
            if verb in ("pull", "login"):
                sys.exit(0)
            if verb == "run":
                name = None
                for i, a in enumerate(argv):
                    if a == "--name" and i + 1 < len(argv):
                        name = argv[i + 1]
                # The container command is everything from the first `bash` token.
                try:
                    bidx = argv.index("bash")
                except ValueError:
                    sys.exit(0)
                inner = argv[bidx:]
                # start_new_session: the trainer runs in its OWN session/group,
                # NOT the caller's process group. This models the dockerd-parenting
                # that makes the wrapper's group SIGKILL unable to reach it.
                child = subprocess.Popen(inner, start_new_session=True)
                if name:
                    with open(os.path.join(STATE, name + ".pid"), "w") as fh:
                        fh.write(str(child.pid))
                # If WE (the docker client) are signalled, the container keeps
                # running — just like a real container detached from a dead CLI.
                child.wait()
                sys.exit(0)
            if verb == "kill":
                name = None
                for a in argv[1:]:
                    if not a.startswith("-"):
                        name = a
                if name:
                    try:
                        with open(os.path.join(STATE, name + ".pid")) as fh:
                            pid = int(fh.read().strip())
                        os.killpg(pid, signal.SIGKILL)
                    except (OSError, ValueError):
                        pass
                sys.exit(0)
            sys.exit(0)
            """
        ),
        encoding="utf-8",
    )
    os.chmod(fake_docker, 0o755)

    # A login shell may reset PATH (Debian's /etc/profile does). Re-prepend the
    # fake-docker dir from ~/.bash_profile so every `bash -lc` in the wrapper
    # resolves `docker` to the fake regardless of profile behaviour.
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    (home_dir / ".bash_profile").write_text(
        f'export PATH={shlex.quote(str(fake_bin))}:"$PATH"\n', encoding="utf-8"
    )

    pid_file = tmp_path / "trainer.pid"
    stubborn = tmp_path / "trainer.py"
    stubborn.write_text(
        "import os, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "with open(sys.argv[1], 'w') as fh:\n"
        "    fh.write(str(os.getpid()))\n"
        "    fh.flush()\n"
        "time.sleep(600)\n",
        encoding="utf-8",
    )

    wrapper = compute_backends_module._build_local_wrapper_command(
        f"python3 {shlex.quote(str(stubborn))} {shlex.quote(str(pid_file))}",
        external_id=external_id,
        container_image="fake/trainer:latest",
        no_progress_timeout_seconds=5,  # the command never touches the progress file
    )
    # Setup sanity: the container is named so the fake can record + target it.
    assert f"--name {shlex.quote(container_name)}" in wrapper

    env = dict(os.environ)
    env["HOME"] = str(home_dir)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"

    def _alive(pid: int) -> bool:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                state = fh.read().rsplit(")", 1)[1].split()[0]
        except OSError:
            return False
        return state != "Z"

    proc = subprocess.Popen(
        ["bash", "-lc", wrapper],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    trainer_pid = None
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            raw = pid_file.read_text().strip() if pid_file.exists() else ""
            if raw:
                trainer_pid = int(raw)
                break
            time.sleep(0.1)
        assert trainer_pid, (
            "the containerized trainer never started (fake docker not on PATH?)"
        )

        # It really is OUTSIDE the wrapper's process group (dockerd-parented) — the
        # precondition that makes the group SIGKILL unable to reach it.
        assert os.getpgid(trainer_pid) != os.getpgid(proc.pid), (
            "the trainer must be in a different process group than the wrapper for "
            "this to model a dockerd-parented workload"
        )

        # The watchdog fires ~5s (no progress), group-TERMs (futile), then must
        # `docker kill` the container ~2s later.
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and _alive(trainer_pid):
            time.sleep(0.2)

        assert not _alive(trainer_pid), (
            "the TERM-ignoring CONTAINERIZED trainer SURVIVED the no-progress "
            "watchdog — the group SIGKILL cannot reach a dockerd-parented process, "
            "so without a `docker kill <name>` escalation a stalled containerized "
            "job keeps running, and keeps billing, after being reported FAILED"
        )

        terminal_path = compute_backends_module._local_terminal_path_for_external_id(
            external_id
        )
        assert os.path.exists(terminal_path), "no terminal record written"
        raw = open(terminal_path, encoding="utf-8").read()
        assert "REASON=no_progress_timeout" in raw, raw
    finally:
        if trainer_pid and _alive(trainer_pid):
            try:
                os.killpg(os.getpgid(trainer_pid), signal.SIGKILL)
            except OSError:
                pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass


def test_no_blocking_time_sleep_in_async_functions():
    """Regression: async coroutines must not call the blocking builtin
    time.sleep(), which freezes the entire event loop. Cooldown waits and any
    other in-coroutine delays must use `await asyncio.sleep(...)` instead.

    This walks the AST of app/compute.py and fails if any `time.sleep(...)`
    call is lexically nested inside an `async def`.
    """
    import ast
    import inspect

    source = inspect.getsource(compute)
    tree = ast.parse(source)

    def _is_time_sleep(call_node):
        func = call_node.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr == "sleep"
            and isinstance(func.value, ast.Name)
            and func.value.id == "time"
        )

    offenders = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self):
            self.async_stack = []

        def visit_AsyncFunctionDef(self, node):
            self.async_stack.append(node.name)
            self.generic_visit(node)
            self.async_stack.pop()

        def visit_FunctionDef(self, node):
            # A sync def nested inside an async def breaks the async context;
            # blocking sleeps there are acceptable, so clear the stack.
            saved = self.async_stack
            self.async_stack = []
            self.generic_visit(node)
            self.async_stack = saved

        def visit_Call(self, node):
            if self.async_stack and _is_time_sleep(node):
                offenders.append((self.async_stack[-1], node.lineno))
            self.generic_visit(node)

    _Visitor().visit(tree)

    assert not offenders, (
        "Blocking time.sleep() found inside async function(s) in compute.py "
        f"(use await asyncio.sleep instead): {offenders}"
    )


# ---------------------------------------------------------------------------
# test_compute_image_registry_iam — validates that compute-image operations
# target the correct Artifact Registry repo and construct the gcloud/docker
# commands that require the artifactregistry.writer / artifactregistry.reader
# IAM bindings (infra/terraform/workload_identity.tf).
# ---------------------------------------------------------------------------


def test_compute_image_registry_iam(monkeypatch, tmp_path):
    """The compute-image flow targets Artifact Registry with the correct commands
    for tag existence checks, image push, and protection tag management — the
    operations gated by artifactregistry.writer and artifactregistry.reader IAM
    bindings on the compute-images repo."""
    from app import compute_backends as cb
    from app import compute_image_build as cib
    from app import storage

    # Create a minimal repo dir with a Dockerfile so build_and_push can read it.
    dockerfile_dir = tmp_path / "repo"
    dockerfile_dir.mkdir()
    (dockerfile_dir / ".blueprint").mkdir()
    (dockerfile_dir / ".blueprint" / "Dockerfile.compute").write_text("FROM scratch\nCOPY . /\n")

    # Configure an Artifact Registry repo (mirrors prod resolution).
    monkeypatch.delenv("COMPUTE_IMAGE_REGISTRY", raising=False)
    monkeypatch.delenv("COMPUTE_IMAGE_REGION", raising=False)
    monkeypatch.setattr(storage, "GCP_PROJECT_ID", "my-gcp-proj")
    ar_repo = cb.resolve_compute_image_registry_repo()
    assert ar_repo == "us-docker.pkg.dev/my-gcp-proj/compute-images"
    assert "docker.pkg.dev" in ar_repo  # Valid AR host.

    commands_run = []

    def _capture_run(cmd, **kw):
        commands_run.append(cmd)

        class _OkProc:
            returncode = 0
            stdout = ""
            stderr = ""

        return _OkProc()

    # ── registry_tag_exists — gcloud artifacts docker images describe against AR ──
    # This is the first read operation against the AR repo (requires reader).
    cib.registry_tag_exists(ar_repo, "test-tag", _run=_capture_run)
    assert any(
        "gcloud" in c and "describe" in c
        for c in commands_run
    ), "Expected gcloud artifacts docker images describe for AR tag existence check"
    assert any(
        ar_repo in str(c) for c in commands_run
    ), f"Expected gcloud describe to target AR repo {ar_repo}"
    commands_run.clear()

    # ── build_and_push (fast-path: tag exists) ──────────────────────────────────
    # Simulates the fast path where the dependency-hash tag already exists.
    result = cib.build_and_push(
        repo_dir=str(dockerfile_dir),
        registry_repo=ar_repo,
        image_name="my-project",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=[],
        _run=_capture_run,
        _tag_exists=lambda *a, **k: True,
        _scan_layers_fn=lambda *a, **k: [],
    )
    assert result.already_present is True
    assert ar_repo in result.pinned_ref
    commands_run.clear()

    # ── build_and_push (full path: build + push) — docker push to AR ──────────
    # Simulates the full build+push path. The orchestrator pushes to the AR
    # repo, which requires artifactregistry.writer on compute_images_repo.
    result = cib.build_and_push(
        repo_dir=str(dockerfile_dir),
        registry_repo=ar_repo,
        image_name="my-project",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=[],
        _run=_capture_run,
        _tag_exists=lambda *a, **k: False,
        _scan_layers_fn=lambda *a, **k: [],
        # Bypass credential scan and network assertions for unit test.
        _assert_network_fn=lambda *a, **k: None,
    )
    assert result.built is True
    assert ar_repo in result.pinned_ref
    push_cmds = [c for c in commands_run if "push" in c]
    assert any(
        ar_repo in str(c) for c in push_cmds
    ), f"Expected docker push to target AR repo {ar_repo}, got {push_cmds}"
    commands_run.clear()

    # ── apply_registry_protection_tag — gcloud artifacts docker tags add ──────
    # Tag management requires artifactregistry.writer on the AR repo.
    digest = "sha256:" + "a" * 64
    tag = cib.apply_registry_protection_tag(ar_repo, digest, _run=_capture_run)
    assert tag is not None
    assert tag.startswith("inuse-")
    add_cmds = [
        c for c in commands_run
        if "gcloud" in str(c) and "artifacts" in str(c) and "tags" in str(c) and "add" in str(c)
    ]
    assert len(add_cmds) >= 1, "Expected gcloud artifacts docker tags add command"
    assert any(
        ar_repo in str(c) for c in add_cmds
    ), f"Expected tags add to target AR repo {ar_repo}"
    commands_run.clear()

    # ── list_registry_protection_tags — gcloud artifacts docker tags list ─────
    # Listing tags requires artifactregistry.reader (or writer) on the AR repo.
    listed = cib.list_registry_protection_tags(ar_repo, _run=_capture_run)
    assert listed == []
    list_cmds = [
        c for c in commands_run
        if "gcloud" in str(c) and "artifacts" in str(c) and "tags" in str(c) and "list" in str(c)
    ]
    assert len(list_cmds) >= 1, "Expected gcloud artifacts docker tags list command"
    assert any(
        ar_repo in str(c) for c in list_cmds
    ), f"Expected tags list to target AR repo {ar_repo}"
    commands_run.clear()

    # ── delete_registry_protection_tag — gcloud artifacts docker tags delete ──
    # Deleting tags requires artifactregistry.writer on the AR repo.
    deleted = cib.delete_registry_protection_tag(
        ar_repo, "inuse-aaaaaaaaaaaaaaaa", _run=_capture_run
    )
    assert deleted is True
    delete_cmds = [
        c for c in commands_run
        if "gcloud" in str(c) and "artifacts" in str(c) and "tags" in str(c) and "delete" in str(c)
    ]
    assert len(delete_cmds) >= 1, "Expected gcloud artifacts docker tags delete command"
    assert any(
        ar_repo in str(c) for c in delete_cmds
    ), f"Expected tags delete to target AR repo {ar_repo}"


# ---------------------------------------------------------------------------
# test_compute_image_builder_least_privilege_contract — drift guard for the
# isolated regional Cloud Build builder IAM/config contract. The builder can
# publish compute images and read its own disposable source bucket, but must
# have NO Secret Manager access, NO platform/runtime credential, and NO access
# to the general agent-artifacts bucket. The orchestrator keeps only the
# permissions to create/get/cancel builds and actAs the builder — while its
# existing compute-image protection-tag write and agent image-pull grants stay
# intact. Config values must be wired into both cloudbuild.yaml (source of
# truth) and cloud_run.tf (drift mirror).
# ---------------------------------------------------------------------------


def _infra_repo_root():
    """Locate the repo root that contains infra/terraform + backend/cloudbuild.yaml.

    Resolves across environments:
      - the Docker backend test harness, which mounts the repo read-only at /repo
        (see scripts/test-backend.sh) while backend/ is the container WORKDIR;
      - a local full checkout, where backend/testing/ is two levels below root.
    Returns None when no such root is reachable (e.g. a stripped-down image with
    neither mount), in which case the drift guard skips rather than false-fails.
    """
    from pathlib import Path

    marker = Path("infra") / "terraform" / "iam.tf"
    candidates = [Path("/repo"), Path(__file__).resolve().parents[2]]
    candidates += list(Path(__file__).resolve().parents)
    seen = set()
    for root in candidates:
        if root in seen:
            continue
        seen.add(root)
        if (root / marker).is_file():
            return root
    return None


def _read_infra_file(relative_name: str) -> str:
    root = _infra_repo_root()
    if root is None:
        pytest.skip("infra/terraform not present in this test image")
    return (root / relative_name).read_text()


def test_compute_image_builder_least_privilege_contract():
    iam = _read_infra_file("infra/terraform/iam.tf")
    wi = _read_infra_file("infra/terraform/workload_identity.tf")
    cb = _read_infra_file("infra/terraform/cloudbuild.tf")

    # The compute-image builder is a dedicated LEAF service account whose only
    # capability is push/pull on the compute-images Artifact Registry repo plus
    # the two grants required to run as a custom SA on Cloud Build
    # (logging.logWriter, storage.objectViewer on the build-source bucket).
    # An off-orchestrator project-image build must reach none of the
    # orchestrator's authority: no Secret Manager, no agent-artifacts bucket,
    # no other project role.
    builder_member = "google_service_account.image_builder_sa.email"

    # (a) The dedicated builder SA exists.
    assert 'resource "google_service_account" "image_builder_sa"' in iam
    assert 'account_id   = "image-builder-sa"' in iam

    # (b) Builder can PUSH built images (artifactregistry.writer) and PULL a
    #     private parent/base image (artifactregistry.reader) — both scoped to the
    #     compute-images repo only, granted in workload_identity.tf.
    writer_block = wi.split(
        'resource "google_artifact_registry_repository_iam_member" "image_builder_compute_images_writer"',
        1,
    )[1].split("resource ", 1)[0]
    assert "google_artifact_registry_repository.compute_images_repo.name" in writer_block
    assert '"roles/artifactregistry.writer"' in writer_block
    assert builder_member in writer_block

    reader_block = wi.split(
        'resource "google_artifact_registry_repository_iam_member" "image_builder_compute_images_reader"',
        1,
    )[1].split("resource ", 1)[0]
    assert "google_artifact_registry_repository.compute_images_repo.name" in reader_block
    assert '"roles/artifactregistry.reader"' in reader_block
    assert builder_member in reader_block

    # (c) Orchestrator can actAs the builder SA (to attach it to a throwaway
    #     builder VM) and nothing more — no new project-level role is granted.
    orch_member = "google_service_account.orchestrator_sa.email"
    actas_block = iam.split(
        'resource "google_service_account_iam_member" "orchestrator_act_as_image_builder"',
        1,
    )[1].split("resource ", 1)[0]
    assert "google_service_account.image_builder_sa.name" in actas_block
    assert '"roles/iam.serviceAccountUser"' in actas_block
    assert orch_member in actas_block

    # (d) Build-source bucket: disposable staging area for Cloud Build source tarballs.
    #     Must be a separate bucket from agent-artifacts; must have a short-retention
    #     lifecycle_rule on the build-sources/ prefix so orphaned objects never accumulate.
    assert 'resource "google_storage_bucket" "compute_build_source"' in cb, (
        "cloudbuild.tf must define google_storage_bucket.compute_build_source"
    )
    bucket_block = cb.split(
        'resource "google_storage_bucket" "compute_build_source"', 1
    )[1].split("resource ", 1)[0]
    assert 'matches_prefix = ["build-sources/"]' in bucket_block, (
        "compute_build_source bucket must have a lifecycle_rule targeting the build-sources/ prefix"
    )
    assert '"Delete"' in bucket_block, (
        "compute_build_source lifecycle_rule must Delete objects (not just transition them)"
    )

    # (e) Orchestrator has Cloud Build editor access (to submit/get/cancel builds)
    #     and objectAdmin on the build-source bucket (to upload and delete staged tarballs).
    assert 'resource "google_project_iam_member" "orchestrator_cloudbuild_editor"' in cb, (
        "cloudbuild.tf must grant orchestrator_sa roles/cloudbuild.builds.editor"
    )
    orch_cb_block = cb.split(
        'resource "google_project_iam_member" "orchestrator_cloudbuild_editor"', 1
    )[1].split("resource ", 1)[0]
    assert '"roles/cloudbuild.builds.editor"' in orch_cb_block
    assert orch_member in orch_cb_block

    assert 'resource "google_storage_bucket_iam_member" "orchestrator_compute_build_source_admin"' in cb, (
        "cloudbuild.tf must grant orchestrator_sa objectAdmin on compute_build_source bucket"
    )
    orch_bkt_block = cb.split(
        'resource "google_storage_bucket_iam_member" "orchestrator_compute_build_source_admin"', 1
    )[1].split("resource ", 1)[0]
    assert '"roles/storage.objectAdmin"' in orch_bkt_block
    assert orch_member in orch_bkt_block
    assert "google_storage_bucket.compute_build_source.name" in orch_bkt_block

    # (f) Builder SA has the two grants required to run as a custom Cloud Build SA:
    #     - logging.logWriter (MANDATORY with logging=CLOUD_LOGGING_ONLY)
    #     - storage.objectViewer on the build-source bucket (to fetch StorageSource)
    assert 'resource "google_project_iam_member" "image_builder_log_writer"' in cb, (
        "cloudbuild.tf must grant image_builder_sa roles/logging.logWriter"
    )
    log_block = cb.split(
        'resource "google_project_iam_member" "image_builder_log_writer"', 1
    )[1].split("resource ", 1)[0]
    assert '"roles/logging.logWriter"' in log_block
    assert builder_member in log_block

    assert 'resource "google_storage_bucket_iam_member" "image_builder_compute_build_source_reader"' in cb, (
        "cloudbuild.tf must grant image_builder_sa storage.objectViewer on compute_build_source bucket"
    )
    bld_bkt_block = cb.split(
        'resource "google_storage_bucket_iam_member" "image_builder_compute_build_source_reader"', 1
    )[1].split("resource ", 1)[0]
    assert '"roles/storage.objectViewer"' in bld_bkt_block
    assert builder_member in bld_bkt_block
    assert "google_storage_bucket.compute_build_source.name" in bld_bkt_block

    # (g) LEAST PRIVILEGE: every resource block that binds a role TO the builder SA
    #     (i.e. names it as the grantee ``member``) grants NO Secret Manager
    #     access and NO access to the agent-artifacts bucket. The builder legitimately
    #     holds exactly four roles: the two Artifact Registry roles (AR reader+writer)
    #     for pushing/pulling compute images, plus logging.logWriter and
    #     storage.objectViewer for running as a custom Cloud Build SA.
    def _blocks_binding_builder(text: str) -> list[str]:
        return [
            "resource " + chunk
            for chunk in text.split("resource ")[1:]
            if builder_member in ("resource " + chunk)
        ]

    granted_roles: list[str] = []
    for body in _blocks_binding_builder(iam) + _blocks_binding_builder(wi) + _blocks_binding_builder(cb):
        assert "secretmanager" not in body, (
            "builder SA must have NO Secret Manager access; offending block:\n" + body
        )
        assert "google_storage_bucket.artifacts" not in body, (
            "builder SA must not be granted access to the agent-artifacts bucket; "
            "offending block:\n" + body
        )
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("role") and '"' in stripped:
                granted_roles.append(stripped.split('"')[1])
    assert sorted(set(granted_roles)) == [
        "roles/artifactregistry.reader",
        "roles/artifactregistry.writer",
        "roles/logging.logWriter",
        "roles/storage.objectViewer",
    ], (
        "builder SA must hold ONLY the four least-privilege roles needed for Cloud Build "
        "(AR reader+writer, logging.logWriter, storage.objectViewer on build-source bucket); "
        f"any other role is a privilege-escalation regression. Found: {sorted(set(granted_roles))}"
    )

    # (h) Pre-existing orchestrator/agent compute-image grants are preserved:
    #     orchestrator keeps its compute-image protection-tag write permission,
    #     and the agent keeps its image-pull (reader) permission.
    assert (
        'resource "google_artifact_registry_repository_iam_member" "orchestrator_compute_images_writer"'
        in wi
    )
    assert (
        'resource "google_artifact_registry_repository_iam_member" "agent_compute_images_reader"'
        in wi
    )


# ── Remote build path (compute_image_remote_build) ─────────────────────────
#
# This module talks to Google APIs directly (Artifact Registry, Cloud Build,
# Storage) and has NEVER shelled out to gcloud, so the historical ``_run`` seam
# these tests passed was silently discarded (**_ignored) — and the placeholder
# ``reg/repo/img`` ref is not a valid Artifact Registry image reference, so the
# real parser correctly rejected it, hiding the mis-mock. The helpers below stub
# the *real* boundary: the AR / Cloud Build clients, with a valid AR ref.

# A valid Artifact Registry image ref: LOCATION-docker.pkg.dev/PROJECT/REPO/IMAGE
_AR_FULL_REPO = "us-central1-docker.pkg.dev/proj/repo/img"


class _FakeDockerImage:
    """Stand-in for an artifactregistry_v1.DockerImage carrying one tag."""

    def __init__(self, tag, digest="sha256:" + "de" * 32):
        self.name = (
            "projects/proj/locations/us-central1/repositories/repo/"
            f"dockerImages/img@{digest}"
        )
        self.tags = [tag]


class _FakeArtifactRegistryClient:
    def __init__(self, images):
        self._images = list(images)

    def list_docker_images(self, request):
        return list(self._images)


class _RaisingArtifactRegistryClient:
    def list_docker_images(self, request):
        raise RuntimeError("artifact registry unreachable")


def _fake_cb_operation(build_id="build-12345"):
    """A Cloud Build create_build() operation whose metadata.build carries an id."""
    op = MagicMock()
    op.metadata.build.id = build_id
    op.metadata.build.log_url = "https://cloudbuild.example/log"
    return op


def _fake_cb_build(status_name, detail=""):
    """A Cloud Build get_build() result with a named status and failure detail."""
    build = MagicMock()
    build.status.name = status_name
    build.failure_info.detail = detail
    return build


def test_remote_registry_tag_exists_present():
    """remote_registry_tag_exists returns True when the AR client finds the tag."""
    from app.compute_image_remote_build import remote_registry_tag_exists

    client = _FakeArtifactRegistryClient([_FakeDockerImage("abc123")])
    assert remote_registry_tag_exists(_AR_FULL_REPO, "abc123", _client=client) is True


def test_remote_registry_tag_exists_absent():
    """remote_registry_tag_exists returns False when the AR client has no match."""
    from app.compute_image_remote_build import remote_registry_tag_exists

    client = _FakeArtifactRegistryClient([_FakeDockerImage("some-other-tag")])
    assert remote_registry_tag_exists(_AR_FULL_REPO, "abc123", _client=client) is False


def test_remote_registry_tag_exists_fail_open_on_error():
    """remote_registry_tag_exists returns False (not-so-present) on any error."""
    from app.compute_image_remote_build import remote_registry_tag_exists

    # Fail-open: a registry error is treated as "not present" so the caller
    # falls through to a build.
    assert (
        remote_registry_tag_exists(
            _AR_FULL_REPO, "abc123", _client=_RaisingArtifactRegistryClient()
        )
        is False
    )


def test_remote_resolve_digest_from_api():
    """_resolve_registry_digest_from_api returns the digest from the AR client."""
    from app.compute_image_remote_build import _resolve_registry_digest_from_api

    digest = "sha256:" + "ba" * 32
    client = _FakeArtifactRegistryClient([_FakeDockerImage("abc123", digest=digest)])
    resolved = _resolve_registry_digest_from_api(_AR_FULL_REPO, "abc123", _client=client)
    assert resolved == digest and resolved.startswith("sha256:")


def test_remote_resolve_digest_from_api_not_found():
    """_resolve_registry_digest_from_api returns None when tag is absent."""
    from app.compute_image_remote_build import _resolve_registry_digest_from_api

    client = _FakeArtifactRegistryClient([_FakeDockerImage("some-other-tag")])
    assert (
        _resolve_registry_digest_from_api(_AR_FULL_REPO, "nonexistent", _client=client)
        is None
    )


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
    """Input credential scan fails the remote build BEFORE Cloud Build is invoked."""
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


def test_remote_build_and_push_cancellation_reaches_cloud_build(tmp_path, monkeypatch):
    """When BuildControl is aborted, the Cloud Build is cancelled.

    Only the GCP clients (Cloud Build + Storage) are stubbed; the real
    _submit_cloud_build -> _poll_cloud_build -> abort-detect -> _cancel_cloud_build
    path runs against the fake client, so the assertion proves the cancellation
    reached the Cloud Build API rather than a mock of the cancellation logic.
    """
    from app import compute_image_remote_build as crb
    from app.compute_image_build import BuildControl, BuildAborted, set_build_control

    monkeypatch.setenv("COMPUTE_IMAGE_BUILDER_SA", "builder-sa@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_LOCATION", "us-central1")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", "proj-compute-build-source")
    monkeypatch.setenv("GCP_PROJECT_ID", "test-proj")

    repo = _write_repo(tmp_path)

    class _FakeCloudBuildClient:
        """Stays WORKING, fires control.abort() after a few polls, then reports
        CANCELLED once cancel_build has been requested."""

        def __init__(self):
            self.create_calls = []
            self.cancel_calls = []
            self.get_count = 0

        def create_build(self, request):
            self.create_calls.append(request)
            return _fake_cb_operation("build-12345")

        def get_build(self, request):
            self.get_count += 1
            if self.cancel_calls:
                # Terminal after cancellation so _wait_for_cloud_build_terminal
                # returns immediately instead of busy-polling.
                return _fake_cb_build("CANCELLED")
            if self.get_count >= 3:
                # In real life the admin endpoint's timeout fires control.abort().
                control = _bci.current_build_control()
                if control is not None:
                    control.abort()
            return _fake_cb_build("WORKING")

        def cancel_build(self, request):
            self.cancel_calls.append(request)

    fake_cb = _FakeCloudBuildClient()
    monkeypatch.setattr(crb, "_cloud_build_client", lambda region=None: fake_cb)
    monkeypatch.setattr(crb, "_REMOTE_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr("google.cloud.storage.Client", lambda *a, **k: MagicMock())

    control = BuildControl()
    token = set_build_control(control)
    try:
        with pytest.raises(BuildAborted, match="cancelled"):
            crb.remote_build_and_push(
                repo_dir=repo,
                registry_repo="reg/repo",
                image_name="img",
                dockerfile=".blueprint/Dockerfile.compute",
                dep_files=["requirements.txt"],
                _tag_exists_fn=lambda *a, **k: False,
            )
    finally:
        _bci.reset_build_control(token)

    # The cancellation reached the Cloud Build job.
    assert len(fake_cb.cancel_calls) >= 1, (
        "Expected a Cloud Build cancel_build call, got "
        f"create={fake_cb.create_calls} cancel={fake_cb.cancel_calls}"
    )


def test_remote_build_and_push_succeeds_without_local_docker(tmp_path, monkeypatch):
    """Remote build succeeds even when docker is unavailable locally.

    This is the key acceptance test: the remote path never touches a local
    docker daemon — it talks only to the Cloud Build / Storage / Artifact
    Registry APIs. Those clients are stubbed; the real remote_build_and_push
    flow (hash -> scan -> package -> submit -> poll SUCCESS -> resolve digest)
    runs end to end, proving a changed image publishes with no local Docker.
    """
    from app import compute_image_remote_build as crb

    monkeypatch.setenv("COMPUTE_IMAGE_BUILDER_SA", "builder-sa@proj.iam.gserviceaccount.com")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_LOCATION", "us-central1")
    monkeypatch.setenv("COMPUTE_IMAGE_BUILD_SOURCE_BUCKET", "proj-compute-build-source")
    monkeypatch.setenv("GCP_PROJECT_ID", "test-proj")

    repo = _write_repo(tmp_path)

    class _FakeCloudBuildClient:
        def __init__(self):
            self.create_calls = []

        def create_build(self, request):
            self.create_calls.append(request)
            return _fake_cb_operation("build-abc")

        def get_build(self, request):
            return _fake_cb_build("SUCCESS")

    fake_cb = _FakeCloudBuildClient()
    monkeypatch.setattr(crb, "_cloud_build_client", lambda region=None: fake_cb)
    monkeypatch.setattr(crb, "_REMOTE_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr("google.cloud.storage.Client", lambda *a, **k: MagicMock())

    digest = "sha256:" + "ff" * 32
    result = crb.remote_build_and_push(
        repo_dir=repo,
        registry_repo="reg/repo",
        image_name="img",
        dockerfile=".blueprint/Dockerfile.compute",
        dep_files=["requirements.txt"],
        _tag_exists_fn=lambda *a, **k: False,
        _resolve_digest_fn=lambda full_repo, tag: digest,
    )

    assert result.built is True
    assert result.already_present is False
    assert result.pinned_ref == f"reg/repo/img@{digest}"
    assert result.pinned_ref.startswith("reg/repo/img@sha256:")
    # The remote path ran: exactly one Cloud Build was submitted (no local docker).
    assert len(fake_cb.create_calls) == 1


def test_remote_build_and_push_not_configured_raises(tmp_path, monkeypatch):
    """When env vars are missing, the remote path raises a clear error."""
    from app.compute_image_remote_build import remote_build_and_push

    # Deliberately do NOT set the required env vars.
    repo = _write_repo(tmp_path)

    with pytest.raises(RuntimeError, match="not configured"):
        remote_build_and_push(
            repo_dir=repo,
            registry_repo="reg/repo",
            image_name="img",
            dockerfile=".blueprint/Dockerfile.compute",
            dep_files=["requirements.txt"],
        )


def test_remote_build_and_push_unchanged_hash_avoids_submission(tmp_path, monkeypatch):
    """When the dep hash exists in the registry, no Cloud Build is submitted.

    This is the acceptance evidence for: 'unchanged hashes avoid submission.'
    """
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

    # The tag_exists function simulates the image already being present.
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
    # No build submission occurred — fast path.
    assert not any("builds submit" in c for c in run_calls)


# ── Honest-success ledger (criterion c-dd715326) ─────────────────────────────

@pytest.mark.asyncio
async def test_aggregate_succeeded_criterion_unmet_not_counted_as_clean_success():
    """A SUCCEEDED job with a measured-but-unmet criterion must NOT appear in
    honest_succeeded.  The operator ledger must never report a clean success
    for a run whose declared success criterion was missed or never measured."""
    from app.compute_jobs_aggregation import aggregate_criterion_summary

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        project = Project(name="Honest Ledger Test", user_id="user-1")
        session.add(project)
        await session.commit()
        await session.refresh(project)

        # Case 1: SUCCEEDED but criterion measured and NOT met (the core bug scenario).
        task_unmet = Task(
            project_id=project.id,
            payloads=[{"type": "compute_experiment", "success_criterion": {"metric": "acc", "operator": ">=", "threshold": 0.9}}],
            status=TaskStatus.COMPLETED,
            plan_output={"compute_result": {"outcome": "SUCCESS", "criterion": {"met": False, "measurable": True, "value": 0.7}}},
        )
        session.add(task_unmet)
        await session.commit()
        await session.refresh(task_unmet)

        job_succeeded_unmet = ComputeJob(
            task_id=task_unmet.id,
            project_id=project.id,
            backend="local-subprocess",
            status=ComputeJobStatus.SUCCEEDED,
            machine_type="e2-standard-2",
            spend_cap_usd=5.0,
            completed_at=_utcnow(),
        )
        session.add(job_succeeded_unmet)

        # Case 2: SUCCEEDED but criterion never measured — met key absent.
        # A real unmeasured run stores no met value in the criterion block.
        task_unmeasured = Task(
            project_id=project.id,
            payloads=[{"type": "compute_experiment"}],
            status=TaskStatus.COMPLETED,
            plan_output={"compute_result": {"outcome": "SUCCESS", "criterion": {"measurable": False}}},
        )
        session.add(task_unmeasured)
        await session.commit()
        await session.refresh(task_unmeasured)

        job_succeeded_unmeasured = ComputeJob(
            task_id=task_unmeasured.id,
            project_id=project.id,
            backend="local-subprocess",
            status=ComputeJobStatus.SUCCEEDED,
            machine_type="e2-standard-2",
            spend_cap_usd=5.0,
            completed_at=_utcnow(),
        )
        session.add(job_succeeded_unmeasured)

        # Case 3: SUCCEEDED with criterion met — this SHOULD count.
        task_met = Task(
            project_id=project.id,
            payloads=[{"type": "compute_experiment"}],
            status=TaskStatus.COMPLETED,
            plan_output={"compute_result": {"outcome": "SUCCESS", "criterion": {"met": True, "measurable": True, "value": 0.95}}},
        )
        session.add(task_met)
        await session.commit()
        await session.refresh(task_met)

        job_succeeded_met = ComputeJob(
            task_id=task_met.id,
            project_id=project.id,
            backend="local-subprocess",
            status=ComputeJobStatus.SUCCEEDED,
            machine_type="e2-standard-2",
            spend_cap_usd=5.0,
            completed_at=_utcnow(),
        )
        session.add(job_succeeded_met)
        await session.commit()

        result = await aggregate_criterion_summary(
            session, project_id=project.id, all_terminal=True
        )

    # Only the genuinely-met SUCCEEDED job counts as honest.
    assert result["honest_succeeded"] == 1, (
        f"Expected 1 honest_succeeded (met=True), got {result['honest_succeeded']}. "
        "SUCCEEDED jobs with unmet or unmeasured criteria must not be counted."
    )
    # Both measured jobs have a non-None criterion row.
    assert result["criterion_measured"] == 2
    # Only one criterion was actually met.
    assert result["criterion_met"] == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_aggregate_all_summary_honest_succeeded_excludes_unmet_and_unmeasured():
    """aggregate_all_compute_jobs_summary must exclude SUCCEEDED jobs with unmet
    or unmeasured criteria from honest_succeeded, mirroring the contract of
    aggregate_criterion_summary."""
    from app.compute_jobs_aggregation import aggregate_all_compute_jobs_summary

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        project = Project(name="All Summary Honest Ledger Test", user_id="user-1")
        session.add(project)
        await session.commit()
        await session.refresh(project)

        # SUCCEEDED but criterion measured and NOT met.
        task_unmet = Task(
            project_id=project.id,
            payloads=[{"type": "compute_experiment"}],
            status=TaskStatus.COMPLETED,
            plan_output={"compute_result": {"outcome": "SUCCESS", "criterion": {"met": False, "measurable": True, "value": 0.7}}},
        )
        session.add(task_unmet)
        await session.commit()
        await session.refresh(task_unmet)
        session.add(ComputeJob(
            task_id=task_unmet.id,
            project_id=project.id,
            backend="local-subprocess",
            status=ComputeJobStatus.SUCCEEDED,
            machine_type="e2-standard-2",
            spend_cap_usd=5.0,
            completed_at=_utcnow(),
        ))

        # SUCCEEDED but criterion never measured (measurable=False, no met key).
        task_unmeasured = Task(
            project_id=project.id,
            payloads=[{"type": "compute_experiment"}],
            status=TaskStatus.COMPLETED,
            plan_output={"compute_result": {"outcome": "SUCCESS", "criterion": {"measurable": False}}},
        )
        session.add(task_unmeasured)
        await session.commit()
        await session.refresh(task_unmeasured)
        session.add(ComputeJob(
            task_id=task_unmeasured.id,
            project_id=project.id,
            backend="local-subprocess",
            status=ComputeJobStatus.SUCCEEDED,
            machine_type="e2-standard-2",
            spend_cap_usd=5.0,
            completed_at=_utcnow(),
        ))

        # SUCCEEDED with criterion met — the only honest success.
        task_met = Task(
            project_id=project.id,
            payloads=[{"type": "compute_experiment"}],
            status=TaskStatus.COMPLETED,
            plan_output={"compute_result": {"outcome": "SUCCESS", "criterion": {"met": True, "measurable": True, "value": 0.95}}},
        )
        session.add(task_met)
        await session.commit()
        await session.refresh(task_met)
        session.add(ComputeJob(
            task_id=task_met.id,
            project_id=project.id,
            backend="local-subprocess",
            status=ComputeJobStatus.SUCCEEDED,
            machine_type="e2-standard-2",
            spend_cap_usd=5.0,
            completed_at=_utcnow(),
        ))
        await session.commit()

        result = await aggregate_all_compute_jobs_summary(session, project_id=project.id, since=None)

    assert result["honest_succeeded"] == 1, (
        f"Expected 1 honest_succeeded (met=True only), got {result['honest_succeeded']}. "
        "SUCCEEDED jobs with unmet or unmeasured criteria must not be counted."
    )
    assert result["criterion_measured"] == 2
    assert result["criterion_met"] == 1

    await engine.dispose()
