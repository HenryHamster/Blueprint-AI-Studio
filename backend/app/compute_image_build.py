#!/usr/bin/env python3
"""Build & publish a per-project compute image, digest-pinned and content-hashed.

This is the PRODUCE side of the images-first compute-dependency direction. It
bakes a container image from a project's *own* lockfiles (the container image
IS the dependency declaration), keying the image tag on a content-hash of the
dependency inputs (lockfiles + Dockerfile), and only rebuilds+pushes when that
hash is new — the same REQHASH philosophy as ``scripts/test-backend.sh``.

Design goals:
  * **Standalone.** Stdlib-only, no ``app`` import, so the operator can run it
    directly on a build host or wire it into a CI trigger.
  * **Importable.** The backend (``app.compute_backends``) imports the pure
    functions here (``compute_dependency_hash``, ``scan_inputs_for_credentials``,
    ``registry_tag_exists``, ``build_and_push``) to drive builds server-side.

Trigger interface (for the operator wiring a CI job on top)
-----------------------------------------------------------
Invoke the CLI with the project repo checked out (or a repo URL + ref):

    python3 scripts/build_compute_image.py \
        --repo-dir /path/to/checkout \
        --registry us-docker.pkg.dev/PROJECT/compute-images \
        --image-name my-project \
        --dockerfile .blueprint/Dockerfile.compute \
        --dep-file requirements.txt --dep-file poetry.lock \
        [--parent python:3.11-slim] \
        [--build-arg KEY=VALUE ...] \
        [--dry-run]

On success it prints the digest-pinned reference
(``registry/repo@sha256:...``) as the last line of stdout, and also emits a
machine-readable JSON summary line prefixed ``RESULT_JSON:``. Exit 0 = built or
already-present; non-zero = validation/credential/build failure. The credential
scanner runs BEFORE any push, so a leaked token fails the build closed.

The CI trigger should run this on every push to the project's default branch
that touches a declared dependency file or the Dockerfile; the hash-gate makes
re-runs on unchanged deps a cheap no-op (registry lookup, no build).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple


# ── Layer-scan memory bounds ─────────────────────────────────────────────────
#
# The credential layer-scan runs against real compute images — a CUDA base plus
# a torch wheel is comfortably multi-GB. Every read it performs is bounded by
# one of these two caps, so peak RSS is O(cap), NOT O(image size).

# Largest single file the scanner will read out of a layer. Anything bigger is
# not a plausible credential store (it is a wheel, a model, a CUDA blob) and is
# skipped without being read.
_MAX_SCANNED_FILE_BYTES = 10 * 1024 * 1024  # 10 MiB

# Largest control document (OCI ``index.json`` / manifest blob) the scanner will
# parse. These are kilobytes in practice; the cap stops a malformed or hostile
# archive from passing off a multi-GB blob as JSON and reintroducing an
# unbounded read through the back door.
_MAX_CONTROL_DOC_BYTES = 4 * 1024 * 1024  # 4 MiB


# ── Content hashing ──────────────────────────────────────────────────────────

# Short hash length for the image tag — 16 hex chars is 64 bits of entropy,
# matching the REQHASH width in scripts/test-backend.sh.
DEP_HASH_LEN = 16


def compute_dependency_hash(inputs: Dict[str, bytes], *, length: int = DEP_HASH_LEN) -> str:
    """Content-hash a set of dependency inputs into a short, stable tag.

    *inputs* maps a logical name (e.g. ``requirements.txt``, ``Dockerfile``) to
    its raw bytes. The hash is order-independent: inputs are sorted by name and
    each contributes ``name\\0<sha256-of-content>`` so that renaming a file or
    reordering the file list changes the hash, but iteration order does not.

    Same inputs -> same hash; any changed byte in any input -> different hash.
    """
    if not inputs:
        raise ValueError("compute_dependency_hash requires at least one input")
    hasher = hashlib.sha256()
    for name in sorted(inputs):
        content = inputs[name]
        if not isinstance(content, (bytes, bytearray)):
            raise TypeError(f"input {name!r} must be bytes, got {type(content).__name__}")
        per_file = hashlib.sha256(bytes(content)).hexdigest()
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(per_file.encode("ascii"))
        hasher.update(b"\x00")
    return hasher.hexdigest()[:length]


# ── Credential scanner (no-platform-creds-in-layers security constraint) ─────

# Substring / regex patterns for platform credentials that must NEVER be baked
# into a published compute-image layer. Matched against both file contents and
# environment-variable values declared in the Dockerfile.
_CREDENTIAL_PATTERNS: List[tuple[str, re.Pattern[bytes]]] = [
    ("github_app_token", re.compile(rb"ghs_[A-Za-z0-9]{20,}")),
    ("github_pat", re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("openai_key", re.compile(rb"sk-[A-Za-z0-9_\-]{20,}")),
    ("anthropic_key", re.compile(rb"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("google_api_key", re.compile(rb"AIza[A-Za-z0-9_\-]{30,}")),
    ("gcp_sa_private_key", re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "gcp_sa_json",
        re.compile(rb'"type"\s*:\s*"service_account"'),
    ),
    ("slack_token", re.compile(rb"xox[baprs]-[A-Za-z0-9\-]{10,}")),
]

# Environment-variable *names* that carry Blueprint platform secrets. A
# Dockerfile that pins any of these to a literal value leaks a platform
# credential into the image and must fail the build.
#
# Kept in sync with scripts/runner_isolation.py's AGENT_ENV_ALLOWLIST
# (credential-bearing subset) and FORBIDDEN_ENV_KEYS via a drift-guard test
# in backend/testing/test_compute.py::test_image_scanner_covers_runner_credential_allowlist.
_SECRET_ENV_NAMES = {
    # Blueprint platform tokens / URLs (also covers FORBIDDEN_ENV_KEYS)
    "ORCHESTRATOR_TOKEN",
    "ADMIN_API_KEY",
    "RUNNER_TOKEN",
    "BLUEPRINT_RUNNER_TOKEN",
    "BLUEPRINT_ORCHESTRATOR_URL",
    "RUNNER_ENROLLMENT_TOKEN",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_INSTALLATION_TOKEN",
    # Anthropic / Claude
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    # OpenAI / Codex
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    # Gemini / Google
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    # Z.ai
    "ZAI_API_KEY",
    # AWS / Bedrock
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    # Other LLM providers
    "DEEPSEEK_API_KEY",
}

# Known on-host secret file paths that must not be COPY/ADD'd into a layer.
_SECRET_PATH_FRAGMENTS = (
    "/secrets/",
    ".blueprint/secrets",
    "service-account.json",
    "sa-key.json",
    "id_rsa",
)

# Dockerfile ENV line, e.g. `ENV FOO=bar` or `ENV FOO bar`.
_ENV_LINE_RE = re.compile(r"^\s*ENV\s+(.*)$", re.IGNORECASE)
# Dockerfile COPY/ADD line.
_COPY_LINE_RE = re.compile(r"^\s*(COPY|ADD)\s+(.*)$", re.IGNORECASE)


def _scan_content_patterns(name: str, content: bytes) -> List[str]:
    """Return findings for raw credential material in *content*."""
    findings: List[str] = []
    for label, pat in _CREDENTIAL_PATTERNS:
        if pat.search(content):
            findings.append(f"{name}: contains {label} credential pattern")
    return findings


def _scan_dockerfile_directives(name: str, content: bytes) -> List[str]:
    """Return findings for secret ENV assignments / secret-path COPY in a Dockerfile."""
    findings: List[str] = []
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — defensive; decode with replace never raises
        return findings
    for raw_line in text.splitlines():
        env_m = _ENV_LINE_RE.match(raw_line)
        if env_m:
            body = env_m.group(1).strip()
            # ENV KEY=VALUE (possibly multiple) or ENV KEY VALUE.
            assignments: List[tuple[str, str]] = []
            if "=" in body:
                for tok in body.split():
                    if "=" in tok:
                        k, _, v = tok.partition("=")
                        assignments.append((k.strip(), v.strip()))
            else:
                parts = body.split(None, 1)
                if len(parts) == 2:
                    assignments.append((parts[0].strip(), parts[1].strip()))
            for k, v in assignments:
                if k in _SECRET_ENV_NAMES and v and not v.startswith("$"):
                    findings.append(
                        f"{name}: ENV {k} assigns a literal platform-secret value"
                    )
        copy_m = _COPY_LINE_RE.match(raw_line)
        if copy_m:
            args = copy_m.group(2)
            findings.extend(
                f"{name}: {copy_m.group(1).upper()} references secret path fragment {frag!r}"
                for frag in _SECRET_PATH_FRAGMENTS
                if frag in args
            )
    return findings


def scan_inputs_for_credentials(
    inputs: Dict[str, bytes], *, dockerfile_names: Sequence[str] = ()
) -> List[str]:
    """Scan build inputs for platform credentials before publishing.

    *inputs* maps a logical name to raw bytes (dependency files + Dockerfile).
    Names in *dockerfile_names* additionally get Dockerfile-directive scanning
    (secret ENV assignments, secret-path COPY/ADD).

    Returns a list of human-readable findings. An empty list means the inputs
    are clean. This is the enforcement point for the
    "no-platform-creds-in-layers" security constraint.
    """
    findings: List[str] = []
    dockerfile_set = set(dockerfile_names)
    for name in sorted(inputs):
        content = inputs[name]
        if not isinstance(content, (bytes, bytearray)):
            raise TypeError(f"input {name!r} must be bytes")
        content = bytes(content)
        findings.extend(_scan_content_patterns(name, content))
        if name in dockerfile_set or name.lower().startswith("dockerfile"):
            findings.extend(_scan_dockerfile_directives(name, content))
    return findings


def _resolve_image_layers(outer: tarfile.TarFile) -> List[Tuple[str, str]]:
    """Resolve the credential-scannable layer tarballs from a ``docker save``
    archive.

    Supports BOTH archive layouts that ``docker save`` can emit:

    * **Classic docker-save layout** — each layer is a ``<hash>/layer.tar``
      member at the top level (referenced by ``manifest.json``'s ``Layers``).
    * **OCI image layout** — the archive is an OCI store: an ``index.json``
      points (by digest) at an image manifest blob under
      ``blobs/<algo>/<hex>``; that manifest's ``layers[].digest`` entries in
      turn resolve to layer blobs under ``blobs/<algo>/<hex>``. Multi-arch
      images nest an OCI index that references per-arch manifests, so index
      references are walked recursively.

    Returns a list of ``(label, member_name)`` tuples — layer *references*, NOT
    layer bytes. Resolving to names keeps the whole scan streaming: a realistic
    CUDA image has multi-GB layers, and materializing even one of them (let
    alone all of them) in memory is what OOM'd the API process. The caller opens
    each layer lazily through :meth:`tarfile.TarFile.extractfile`, which reads
    through the underlying file handle without buffering the member.

    Only the small JSON control documents (``index.json``, manifest blobs) are
    read into memory — they are kilobytes, and the OCI walk needs them parsed.

    An **empty list** means NO recognizable layers were found — the caller MUST
    treat that as a fail-closed error (the scan would otherwise silently pass
    unexamined layers).
    """
    members = {m.name: m for m in outer.getmembers()}

    def _read_control(name: str) -> bytes | None:
        """Read a small control document (index/manifest JSON) into memory.

        Guarded by a size cap so a hostile archive cannot pass off a multi-GB
        blob as ``index.json`` and reintroduce the unbounded read this function
        exists to avoid.
        """
        m = members.get(name)
        if m is None or not m.isfile():
            return None
        if m.size > _MAX_CONTROL_DOC_BYTES:
            return None
        fh = outer.extractfile(m)
        if fh is None:
            return None
        return fh.read(_MAX_CONTROL_DOC_BYTES)

    layers: List[Tuple[str, str]] = []

    # ── Classic docker-save layout: <hash>/layer.tar ─────────────────────────
    for name, m in members.items():
        if m.isfile() and name.endswith("layer.tar"):
            layers.append((name, name))
    if layers:
        return layers

    # ── OCI image layout: index.json → manifest blob → layer blobs ───────────
    index_bytes = _read_control("index.json")
    if index_bytes is None:
        return layers  # neither classic nor OCI — caller fails closed

    def _blob_path(digest: str) -> Optional[str]:
        # An OCI digest is "<algo>:<hex>"; the blob lives at blobs/<algo>/<hex>.
        if not isinstance(digest, str) or ":" not in digest:
            return None
        algo, _, hexd = digest.partition(":")
        if not algo or not hexd:
            return None
        return f"blobs/{algo}/{hexd}"

    seen: Set[str] = set()

    def _walk_manifest(digest: str) -> None:
        if digest in seen:
            return
        seen.add(digest)
        path = _blob_path(digest)
        if path is None:
            return
        raw = _read_control(path)
        if raw is None:
            return
        try:
            doc = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(doc, dict):
            return
        # Nested OCI index (multi-arch) → recurse into referenced manifests.
        for sub in doc.get("manifests", []) or []:
            if isinstance(sub, dict) and isinstance(sub.get("digest"), str):
                _walk_manifest(sub["digest"])
        # Image manifest → collect the layer blobs it references (by NAME).
        for layer in doc.get("layers", []) or []:
            if not isinstance(layer, dict):
                continue
            lpath = _blob_path(layer.get("digest"))
            if lpath is None:
                continue
            m = members.get(lpath)
            if m is not None and m.isfile():
                layers.append((f"blob:{lpath}", lpath))

    try:
        index_doc = json.loads(index_bytes)
    except (ValueError, UnicodeDecodeError):
        return layers
    if isinstance(index_doc, dict):
        for man in index_doc.get("manifests", []) or []:
            if isinstance(man, dict) and isinstance(man.get("digest"), str):
                _walk_manifest(man["digest"])

    return layers


def scan_image_layers_for_credentials(
    image_ref: str, *, _run=subprocess.run
) -> List[str]:
    """Scan a locally-built image's layers for platform credentials.

    Exports the image via ``docker save``, then walks every file in every
    layer tarball and applies the same credential patterns used for build
    inputs.  This catches secrets baked in at build-time (e.g. via
    ``RUN curl http://secrets-server/token > /root/.creds`` or a pip package
    that installs a credential helper) that are invisible in the source inputs.

    **Streaming, never buffered.** ``docker save`` writes STRAIGHT TO A TEMP FILE
    (``-o``) instead of a pipe the parent drains into memory, and layers are read
    lazily through :meth:`tarfile.TarFile.extractfile` rather than materialized
    as ``bytes``. A realistic CUDA compute image is multiple GB; the previous
    ``capture_output=True`` + ``io.BytesIO(proc.stdout)`` + per-layer ``bytes``
    shape held the archive in RAM two or three times over and OOM'd the API
    process that called this. Peak memory is now bounded by the largest single
    scanned FILE (hard-capped at 10 MiB below), not by the image size.

    Both archive layouts ``docker save`` can produce are handled — the classic
    ``<hash>/layer.tar`` layout and the OCI ``index.json`` + ``blobs/`` store
    (see :func:`_resolve_image_layers`). When the archive yields NO recognizable
    layers, the scan **fails closed** (raises ``RuntimeError``) rather than
    silently passing unexamined content.

    Returns a list of human-readable findings (empty = clean).  Network/auth
    errors in ``docker save`` are propagated as ``RuntimeError`` so callers
    can treat infrastructure failures distinctly from credential findings.
    """
    with tempfile.TemporaryDirectory(prefix="compute-image-scan-") as tmpdir:
        archive_path = os.path.join(tmpdir, "image.tar")
        try:
            # `-o` streams the archive to disk inside the daemon/CLI. Nothing is
            # piped back through this process, so a multi-GB image costs disk,
            # not RAM. stdout/stderr stay captured for error reporting only —
            # with `-o` they carry progress/error text, not the archive.
            proc = _run(
                ["docker", "save", "-o", archive_path, image_ref],
                capture_output=True,
                timeout=600,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"docker save failed: {exc}") from exc

        if proc.returncode != 0:
            raw_err = proc.stderr or b""
            if isinstance(raw_err, str):
                stderr = raw_err[-1000:]
            else:
                stderr = raw_err.decode("utf-8", errors="replace")[-1000:]
            raise RuntimeError(f"docker save failed (exit {proc.returncode}): {stderr}")

        if not os.path.isfile(archive_path):
            # Fail CLOSED: `docker save` reported success but produced no
            # archive. Returning "clean" here would skip the credential gate.
            raise RuntimeError(
                f"docker save produced no archive for {image_ref!r} — failing closed"
            )

        findings: List[str] = []
        try:
            # Opened from a PATH: the file handle is seekable, so extractfile()
            # returns a seekable stream and oversized members can be skipped by
            # seeking past them rather than reading them.
            with tarfile.open(archive_path, mode="r:*") as outer:
                layers = _resolve_image_layers(outer)
                if not layers:
                    # Fail CLOSED: an archive with no classic layer.tar members
                    # and no resolvable OCI blob layers is unscannable, not
                    # clean. A silent empty-findings pass here would let an
                    # OCI-layout image (or a malformed/unexpected archive) skip
                    # the credential gate.
                    raise RuntimeError(
                        "credential layer-scan found no recognizable layers in the "
                        f"docker save archive for {image_ref!r} (neither classic "
                        "layer.tar members nor a resolvable OCI index.json/blobs "
                        "layout) — failing closed"
                    )
                for label, member_name in layers:
                    member = outer.getmember(member_name)
                    layer_stream = outer.extractfile(member)
                    if layer_stream is None:
                        continue
                    try:
                        # Nested tar read THROUGH the outer archive's handle —
                        # the layer is never materialized as a bytes object.
                        with tarfile.open(
                            fileobj=layer_stream, mode="r:*"
                        ) as layer:
                            for fmember in layer:
                                if not fmember.isfile():
                                    continue
                                # Skip files that are almost certainly not
                                # text/credential stores. The upper bound also
                                # caps this scan's peak memory: no single read
                                # below can exceed it.
                                if (
                                    fmember.size == 0
                                    or fmember.size > _MAX_SCANNED_FILE_BYTES
                                ):
                                    continue
                                ffh = layer.extractfile(fmember)
                                if ffh is None:
                                    continue
                                content = ffh.read(_MAX_SCANNED_FILE_BYTES)
                                flabel = f"layer:{label}!{fmember.name}"
                                findings.extend(
                                    _scan_content_patterns(flabel, content)
                                )
                    except tarfile.TarError:
                        # Corrupted layer — skip rather than crashing.
                        pass
                    finally:
                        layer_stream.close()
        except tarfile.TarError as exc:
            raise RuntimeError(f"failed to parse docker save output: {exc}") from exc

        return findings


# ── Registry lookup + build/push orchestration ───────────────────────────────


def registry_tag_exists(
    registry_repo: str, tag: str, *, _run=subprocess.run
) -> bool:
    """Whether ``registry_repo:tag`` already exists in the registry.

    Uses ``docker manifest inspect`` (works for Artifact Registry with docker
    credentials configured). Any lookup error (network, auth, absent tag) is
    treated as "not present" so the caller falls through to a build — the build
    itself is idempotent-by-hash, so a false negative only costs one rebuild.
    """
    ref = f"{registry_repo}:{tag}"
    try:
        proc = _run(
            ["docker", "manifest", "inspect", ref],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


# ── DB-aware GC protection tags ──────────────────────────────────────────────
#
# Registry retention (Terraform cleanup_policy on the compute-images repo) is
# purely time/count-based and cannot query the orchestrator DB. To stop a digest
# a live/recent compute job still depends on from being GC'd, the reaper stamps a
# durable ``inuse-<shorthash>`` tag on the referenced digest; a matching KEEP
# cleanup_policy (``keep-inuse-tagged``) shields anything so tagged from
# deletion. These helpers apply / list / delete those protection tags via
# ``gcloud artifacts docker tags`` and FAIL OPEN — any error (no gcloud, no
# creds, transient API) is swallowed so the reaper cycle never breaks; a missed
# protection tag only costs a chance at reclaim on the next hourly sweep.

_PROTECTION_TAG_PREFIX = "inuse-"


def protection_tag_for_digest(digest: str) -> str:
    """Deterministic ``inuse-<shorthash>`` protection tag for a ``sha256:`` digest.

    The 16-hex-char shorthash is taken from the digest hex so the tag is stable
    across sweeps (idempotent re-tagging) and the reaper can decide which tags to
    prune purely from the tag name, without resolving each tag back to a digest.
    """
    short = digest.split(":", 1)[-1][:16]
    return f"{_PROTECTION_TAG_PREFIX}{short}"


def apply_registry_protection_tag(
    full_repo: str, digest: str, *, _run=subprocess.run
) -> str | None:
    """Tag ``full_repo@digest`` with its ``inuse-`` protection tag; None on failure.

    Idempotent: re-adding an existing tag pointed at the same digest is a no-op
    for the KEEP policy. Fails open — returns None on any gcloud/OS error.
    """
    tag = protection_tag_for_digest(digest)
    src = f"{full_repo}@{digest}"
    dst = f"{full_repo}:{tag}"
    try:
        proc = _run(
            ["gcloud", "artifacts", "docker", "tags", "add", src, dst],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return tag if proc.returncode == 0 else None


def list_registry_protection_tags(
    full_repo: str, *, _run=subprocess.run
) -> List[str]:
    """Short ``inuse-`` tag names currently present on ``full_repo`` (fail open -> [])."""
    try:
        proc = _run(
            ["gcloud", "artifacts", "docker", "tags", "list", full_repo, "--format=json"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    try:
        entries = json.loads(proc.stdout or "[]")
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    tags: List[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("tag")
        if not isinstance(raw, str) or not raw:
            continue
        # gcloud emits a fully-qualified tag path; keep just the short tag name.
        short = raw.rsplit(":", 1)[-1] if ":" in raw else raw.rsplit("/", 1)[-1]
        if short.startswith(_PROTECTION_TAG_PREFIX):
            tags.append(short)
    return tags


def delete_registry_protection_tag(
    full_repo: str, tag: str, *, _run=subprocess.run
) -> bool:
    """Delete a single ``inuse-`` protection tag from ``full_repo`` (fail open -> False)."""
    try:
        proc = _run(
            [
                "gcloud",
                "artifacts",
                "docker",
                "tags",
                "delete",
                f"{full_repo}:{tag}",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _resolve_pushed_digest(
    registry_repo: str, tag: str, *, _run=subprocess.run
) -> str | None:
    """Resolve the ``sha256:...`` digest of a pushed tag, or None on failure."""
    ref = f"{registry_repo}:{tag}"
    try:
        proc = _run(
            [
                "docker",
                "image",
                "inspect",
                "--format={{index .RepoDigests 0}}",
                ref,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    # Format: registry/repo@sha256:...
    _, sep, digest = out.partition("@")
    if sep and digest.startswith("sha256:"):
        return digest
    return None


@dataclass
class BuildResult:
    """Outcome of a build_and_push invocation."""

    pinned_ref: str
    tag: str
    dep_hash: str
    already_present: bool
    built: bool
    findings: List[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "pinned_ref": self.pinned_ref,
                "tag": self.tag,
                "dep_hash": self.dep_hash,
                "already_present": self.already_present,
                "built": self.built,
                "findings": self.findings,
            }
        )


class CredentialLeakError(RuntimeError):
    """Raised when the credential scanner flags build inputs before push."""


def build_and_push(
    *,
    repo_dir: str,
    registry_repo: str,
    image_name: str,
    dockerfile: str,
    dep_files: Sequence[str],
    parent: str | None = None,
    build_args: Dict[str, str] | None = None,
    dry_run: bool = False,
    _run=subprocess.run,
    _tag_exists=None,
    _scan_layers_fn=None,
) -> BuildResult:
    """Content-hash the deps, skip-if-present, else scan + build + push.

    Returns a :class:`BuildResult` carrying the digest-pinned reference. Raises
    :class:`CredentialLeakError` if the credential scanner flags any input, and
    :class:`FileNotFoundError` for a missing Dockerfile or declared dep file.
    ``dry_run`` performs the hash + scan + presence check but never builds or
    pushes (the pinned_ref is best-effort/tag-only in that mode).
    ``_scan_layers_fn`` is an injectable hook for tests; defaults to
    :func:`scan_image_layers_for_credentials`.
    """
    tag_exists = _tag_exists if _tag_exists is not None else registry_tag_exists
    build_args = dict(build_args or {})

    dockerfile_path = os.path.join(repo_dir, dockerfile)
    if not os.path.isfile(dockerfile_path):
        raise FileNotFoundError(f"Dockerfile not found: {dockerfile_path}")

    # Collect hash inputs: the Dockerfile plus each declared dep file.
    inputs: Dict[str, bytes] = {}
    with open(dockerfile_path, "rb") as fh:
        inputs["Dockerfile"] = fh.read()
    for rel in dep_files:
        dep_path = os.path.join(repo_dir, rel)
        if not os.path.isfile(dep_path):
            raise FileNotFoundError(f"declared dependency file not found: {dep_path}")
        with open(dep_path, "rb") as fh:
            inputs[rel] = fh.read()

    dep_hash = compute_dependency_hash(inputs)
    full_repo = f"{registry_repo}/{image_name}"

    # Fast path: image already present for this dependency hash.
    if tag_exists(full_repo, dep_hash, _run=_run):
        digest = _resolve_pushed_digest(full_repo, dep_hash, _run=_run)
        pinned = f"{full_repo}@{digest}" if digest else f"{full_repo}:{dep_hash}"
        return BuildResult(
            pinned_ref=pinned,
            tag=dep_hash,
            dep_hash=dep_hash,
            already_present=True,
            built=False,
        )

    # Security gate: never publish a layer carrying platform credentials.
    findings = scan_inputs_for_credentials(inputs, dockerfile_names=["Dockerfile"])
    if findings:
        raise CredentialLeakError(
            "credential scan failed before push: " + "; ".join(findings)
        )

    if dry_run:
        return BuildResult(
            pinned_ref=f"{full_repo}:{dep_hash}",
            tag=dep_hash,
            dep_hash=dep_hash,
            already_present=False,
            built=False,
            findings=[],
        )

    # Build.
    build_cmd = [
        "docker",
        "build",
        "-f",
        dockerfile_path,
        "-t",
        f"{full_repo}:{dep_hash}",
    ]
    if parent:
        build_args.setdefault("PARENT_IMAGE", parent)
    for k, v in sorted(build_args.items()):
        build_cmd.extend(["--build-arg", f"{k}={v}"])
    build_cmd.append(repo_dir)

    proc = _run(build_cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker build failed (exit {proc.returncode}): {(proc.stderr or '')[-2000:]}"
        )

    # Security gate: scan built image layers for credentials BEFORE pushing.
    # This catches secrets baked in at build-time (e.g. via RUN curl or pip
    # packages) that are invisible in the source inputs scanned above.
    image_ref = f"{full_repo}:{dep_hash}"
    _do_scan_layers = _scan_layers_fn if _scan_layers_fn is not None else scan_image_layers_for_credentials
    layer_findings = _do_scan_layers(image_ref, _run=_run)
    if layer_findings:
        raise CredentialLeakError(
            "credential scan of built image layers failed before push: "
            + "; ".join(layer_findings)
        )

    # Push.
    push_proc = _run(
        ["docker", "push", f"{full_repo}:{dep_hash}"],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if push_proc.returncode != 0:
        raise RuntimeError(
            f"docker push failed (exit {push_proc.returncode}): {(push_proc.stderr or '')[-2000:]}"
        )

    digest = _resolve_pushed_digest(full_repo, dep_hash, _run=_run)
    pinned = f"{full_repo}@{digest}" if digest else f"{full_repo}:{dep_hash}"
    return BuildResult(
        pinned_ref=pinned,
        tag=dep_hash,
        dep_hash=dep_hash,
        already_present=False,
        built=True,
    )


def _parse_build_args(pairs: Sequence[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--build-arg must be KEY=VALUE, got {pair!r}")
        k, _, v = pair.partition("=")
        out[k.strip()] = v
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", required=True, help="Path to the project checkout")
    parser.add_argument("--registry", required=True, help="Registry repo, e.g. us-docker.pkg.dev/PROJECT/compute-images")
    parser.add_argument("--image-name", required=True, help="Image name within the registry repo")
    parser.add_argument("--dockerfile", default=".blueprint/Dockerfile.compute")
    parser.add_argument("--dep-file", action="append", default=[], help="Dependency file (repeatable)")
    parser.add_argument("--parent", default=None)
    parser.add_argument("--build-arg", action="append", default=[], help="KEY=VALUE (repeatable)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        build_args = _parse_build_args(args.build_arg)
        result = build_and_push(
            repo_dir=args.repo_dir,
            registry_repo=args.registry,
            image_name=args.image_name,
            dockerfile=args.dockerfile,
            dep_files=args.dep_file,
            parent=args.parent,
            build_args=build_args,
            dry_run=args.dry_run,
        )
    except CredentialLeakError as e:
        print(f"ERROR (credential leak): {e}", file=sys.stderr)
        return 3
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"ERROR (build): {e}", file=sys.stderr)
        return 1

    print(f"RESULT_JSON:{result.to_json()}")
    print(result.pinned_ref)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
