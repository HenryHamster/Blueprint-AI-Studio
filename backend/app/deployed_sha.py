"""Best-effort runtime resolver for the deployed git SHA.

The orchestrator image is deployed as
``us-central1-docker.pkg.dev/<project>/blueprint-repo/blueprint-orchestrator:<git-sha>``
— the container image TAG is exactly the deployed git commit SHA. There is no
``DEPLOY_SHA`` env var set by the deploy pipeline and the git directory is
excluded from the image, so the only in-process source of truth is the running
Cloud Run revision's container image reference.

This module looks up the serving revision (named by ``K_REVISION``) via the
Cloud Run Admin API and parses the SHA out of ``containers[0].image``. Every
failure mode degrades to ``None`` so callers keep their existing default and
startup is never blocked.
"""

import logging
import os

logger = logging.getLogger(__name__)

CLOUD_RUN_SERVICE = "blueprint-orchestrator"
CLOUD_RUN_REGION = "us-central1"


def _parse_sha_from_image(image: str | None) -> str | None:
    """Extract the git SHA from a container image reference.

    The image looks like ``.../blueprint-orchestrator:<git-sha>``; the tag after
    the final ``:`` is the deployed commit. Returns ``None`` for a missing image,
    a reference with no tag, or a tag that does not look like a git SHA (e.g.
    ``latest``) so we never report a non-SHA value as the deployed commit.
    """
    if not image or ":" not in image:
        return None
    tag = image.rsplit(":", 1)[1]
    if len(tag) >= 7 and all(c in "0123456789abcdef" for c in tag.lower()):
        return tag
    return None


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


async def resolve_deployed_sha() -> str | None:
    """Resolve the git SHA the orchestrator is actually serving.

    Returns ``None`` (so callers keep their existing default) when not running on
    Cloud Run (``K_REVISION`` unset), when the project id is missing, when the
    lookup fails for any reason, or when the image tag does not look like a SHA.
    Never raises.
    """
    revision = os.getenv("K_REVISION")
    if not revision:
        return None  # not on Cloud Run (local/dev/non-cloud run)

    project_id = os.getenv("GCP_PROJECT_ID")
    if not project_id or project_id == "your-gcp-project-id":
        return None

    try:
        image = await _fetch_revision_image(
            project_id, CLOUD_RUN_REGION, CLOUD_RUN_SERVICE, revision
        )
    except Exception:
        logger.warning(
            "[DEPLOY] Could not resolve deployed SHA from Cloud Run revision "
            f"{revision!r}; falling back to default",
            exc_info=True,
        )
        return None

    sha = _parse_sha_from_image(image)
    if sha:
        logger.info(f"[DEPLOY] Resolved deployed SHA {sha[:8]} from image {image!r}")
    else:
        logger.warning(
            f"[DEPLOY] Cloud Run revision {revision!r} image {image!r} has no "
            "SHA-looking tag; falling back to default"
        )
    return sha
