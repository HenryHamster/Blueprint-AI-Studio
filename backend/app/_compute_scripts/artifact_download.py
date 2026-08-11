"""Predecessor-artifact rehydrator invoked by the GCP compute-job VM startup script.

Downloads a prior compute job's durable artifact bundle from a signed GET URL and
extracts it, path-traversal-safe, into the destination artifact dir BEFORE the
job's command runs — the GCP analogue of the runner/local cross-job checkpoint
resume. Members with absolute paths or ``..`` traversal are SKIPPED (never
written), as are symlink/hardlink members whose targets could escape the dir.

argv[1]: signed GET URL for the predecessor bundle (.tar.gz)
argv[2]: destination directory to extract into (the BLUEPRINT_ARTIFACT_DIR path)

Exit 0 on success (bundle rehydrated), non-zero on any download/extract failure so
the caller can fail the job LOUDLY instead of running against an empty workspace.
"""

import io
import os
import sys
import tarfile
import urllib.request


def _is_safe_member(name: str) -> bool:
    if not name:
        return False
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("~"):
        return False
    if os.path.splitdrive(normalized)[0]:
        return False
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    return ".." not in parts


def main() -> int:
    url = sys.argv[1]
    dest_dir = sys.argv[2]
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=120) as response:
        bundle = response.read()
    os.makedirs(dest_dir, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.islnk() or member.issym():
                continue
            if not _is_safe_member(member.name):
                sys.stderr.write(
                    "skipping unsafe artifact-bundle member: %r\n" % member.name
                )
                continue
            tar.extract(member, path=dest_dir, filter="data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
