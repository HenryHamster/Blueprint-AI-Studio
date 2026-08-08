"""Best-effort runtime resolver for the deployed git SHA.

The orchestrator image is built and deployed tagged with the git commit:
``us-central1-docker.pkg.dev/<project>/blueprint-repo/blueprint-orchestrator:<git-sha>``
(see ``.github/workflows/deploy.yml``). The deploy pipeline ALSO stamps that
commit onto the Cloud Run revision as the ``DEPLOY_SHA`` env var, which is the
authoritative in-process source of truth — the running container knows exactly
which commit it is without any API call.

The env var is the primary source. The image-ref fallbacks exist for older
revisions deployed before ``DEPLOY_SHA`` was wired and as defense-in-depth:

  * The **serving revision's** ``containers[0].image`` is NOT reliable — Cloud
    Run resolves the deployed ``:<git-sha>`` tag to an immutable image DIGEST
    (``...@sha256:<64-hex>``) in the revision, so the revision ref usually
    carries no git tag at all. A 64-hex digest is all-hex and would naively
    "look like" a SHA, so :func:`_parse_sha_from_image` discards the digest
    BEFORE looking for a tag and never reports a digest as the commit.
  * The **service template** image (``service.template.containers[0].image``)
    DOES retain the deployed ``:<git-sha>`` tag, so it is the second fallback.

Every failure mode degrades to ``None`` so callers keep their existing default
and startup is never blocked.
"""

import logging
import os

logger = logging.getLogger(__name__)

CLOUD_RUN_SERVICE = "blueprint-orchestrator"
CLOUD_RUN_REGION = "us-central1"

# A git commit SHA is 7-40 lowercase hex chars. The upper bound is what rejects
# a 64-hex container image DIGEST from ever being mistaken for a commit.
_GIT_SHA_MIN_LEN = 7
_GIT_SHA_MAX_LEN = 40


def _looks_like_git_sha(value: str | None) -> bool:
    """True iff ``value`` is a plausible git commit SHA (7-40 hex chars).

    The 7-40 length window deliberately EXCLUDES a 64-hex image digest, so a
    digest can never be reported as the deployed commit even if it reaches a
    code path that skipped the ``@sha256:`` stripping in
    :func:`_parse_sha_from_image`.
    """
    if not value:
        return False
    if not (_GIT_SHA_MIN_LEN <= len(value) <= _GIT_SHA_MAX_LEN):
        return False
    return all(c in "0123456789abcdef" for c in value.lower())


def _parse_sha_from_image(image: str | None) -> str | None:
    """Extract the git SHA tag from a container image reference.

    Handles three shapes:
      * ``repo:<git-sha>``             -> the tag, if it looks like a git SHA
      * ``repo@sha256:<digest>``       -> ``None`` (digest-pinned, no git tag)
      * ``repo:<tag>@sha256:<digest>`` -> the tag (parsed from the pre-``@`` part)

    Returns ``None`` for a missing image, a digest-only reference, a reference
    with no tag, or a tag that does not look like a git SHA (``latest``, or a
    64-hex digest). Never returns a digest.
    """
    if not image:
        return None
    # Strip any ``@sha256:<digest>`` pin first — the digest is all-hex and would
    # otherwise pass the git-SHA check below.
    ref = image.split("@", 1)[0]
    # Only the LAST path component can carry a tag; a registry host may include a
    # ``:port`` (e.g. ``host:5000/repo``) that must not be mistaken for a tag.
    last_segment = ref.rsplit("/", 1)[-1]
    if ":" not in last_segment:
        return None
    tag = last_segment.rsplit(":", 1)[1]
    return tag if _looks_like_git_sha(tag) else None


async def _fetch_revision_image(
    project_id: str, region: str, service: str, revision: str
) -> str | None:
    """Fetch the container image reference of a Cloud Run revision.

    Isolated from :func:`resolve_deployed_sha` so tests can mock the network call
    without needing the Cloud Run SDK installed. Returns the image string or
    ``None`` if the revision has no containers.
    """
    from google.cloud import run_v2

    client = run_v2.RevisionsAsyncClient()
    name = client.revision_path(project_id, region, service, revision)
    rev = await client.get_revision(name=name)
    if not rev.containers:
        return None
    return rev.containers[0].image


async def _fetch_service_image(
    project_id: str, region: str, service: str
) -> str | None:
    """Fetch the container image reference of a Cloud Run service's template.

    Unlike a revision (whose image Cloud Run digest-pins), the service template
    retains the ``:<git-sha>`` tag the deploy passed to ``gcloud run deploy``.
    Isolated for the same test-mockability reason as :func:`_fetch_revision_image`.
    """
    from google.cloud import run_v2

    client = run_v2.ServicesAsyncClient()
    name = client.service_path(project_id, region, service)
    svc = await client.get_service(name=name)
    template = getattr(svc, "template", None)
    containers = getattr(template, "containers", None) if template else None
    if not containers:
        return None
    return containers[0].image


def get_serving_sha() -> str | None:
    """Return the serving git SHA synchronously from the DEPLOY_SHA env var.

    Fast and side-effect-free — reads the env var the deploy pipeline stamps on
    every revision. Returns None when DEPLOY_SHA is absent or not a valid SHA.
    """
    env_sha = os.getenv("DEPLOY_SHA")
    return env_sha if _looks_like_git_sha(env_sha) else None


async def resolve_deployed_sha() -> str | None:
    """Resolve the git SHA the orchestrator is actually serving.

    Resolution order (first hit wins):
      1. ``DEPLOY_SHA`` env var — stamped on the revision by the deploy pipeline.
      2. The serving revision's image tag (usually a digest, so usually misses).
      3. The service template's image tag (retains the deployed ``:<git-sha>``).

    Returns ``None`` (so callers keep their existing default) when not running on
    Cloud Run (``K_REVISION`` unset), when the project id is missing, when every
    lookup fails, or when no source yields a git-SHA-looking value. Never raises.
    """
    # 1. Authoritative: the deploy pipeline stamps the commit on the revision env.
    env_sha = os.getenv("DEPLOY_SHA")
    if _looks_like_git_sha(env_sha):
        logger.info(f"[DEPLOY] Resolved deployed SHA {env_sha[:8]} from DEPLOY_SHA env")
        return env_sha

    revision = os.getenv("K_REVISION")
    if not revision:
        # Not on Cloud Run (local/dev/non-cloud run). In local-production the
        # role launchers fail closed unless they can resolve a deploy identity,
        # so reaching this point means the environment is misconfigured — the
        # singleton's serving SHA would read "unknown" and post-promotion
        # serving confirmation would time out against a stale row. Log it
        # loudly; still return None so startup is never blocked.
        if os.getenv("BLUEPRINT_DEPLOYMENT_MODE") == "local-production":
            logger.error(
                "[DEPLOY] Serving in local-production mode with no resolvable "
                "deploy identity: DEPLOY_SHA is absent or invalid and the role "
                "launcher did not derive one from the image. The deployment "
                "singleton will report serving SHA 'unknown', so post-promotion "
                "serving confirmation will not confirm this revision."
            )
        return None  # not on Cloud Run (local/dev/non-cloud run)

    project_id = os.getenv("GCP_PROJECT_ID")
    if not project_id or project_id == "your-gcp-project-id":
        return None

    # 2. Serving revision image — Cloud Run digest-pins this, so it usually has
    #    no git tag; a tag-pinned revision (other deploy modes) still resolves.
    image: str | None = None
    try:
        image = await _fetch_revision_image(
            project_id, CLOUD_RUN_REGION, CLOUD_RUN_SERVICE, revision
        )
    except Exception:
        logger.warning(
            "[DEPLOY] Could not resolve deployed SHA from Cloud Run revision "
            f"{revision!r}; trying the service template",
            exc_info=True,
        )
    sha = _parse_sha_from_image(image)
    if sha:
        logger.info(f"[DEPLOY] Resolved deployed SHA {sha[:8]} from revision image {image!r}")
        return sha

    # 3. Service template image — retains the deployed ``:<git-sha>`` tag.
    svc_image: str | None = None
    try:
        svc_image = await _fetch_service_image(
            project_id, CLOUD_RUN_REGION, CLOUD_RUN_SERVICE
        )
    except Exception:
        logger.warning(
            "[DEPLOY] Could not resolve deployed SHA from Cloud Run service "
            f"{CLOUD_RUN_SERVICE!r} template; falling back to default",
            exc_info=True,
        )
    sha = _parse_sha_from_image(svc_image)
    if sha:
        logger.info(
            f"[DEPLOY] Resolved deployed SHA {sha[:8]} from service template "
            f"image {svc_image!r}"
        )
        return sha

    logger.warning(
        f"[DEPLOY] Could not resolve a git SHA for revision {revision!r} "
        f"(revision image={image!r}, service image={svc_image!r}); "
        "falling back to default"
    )
    return None
