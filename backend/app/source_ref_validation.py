"""Validation of research finding source file references at ingestion time.

Validates that file refs in source objects actually exist and that line
numbers are within bounds. Refs that fail validation are marked with
valid: false or dropped, with warnings logged. The system degrades gracefully
on validation failures — does not crash.
"""

import asyncio
import logging
import re
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _parse_ref(ref: str) -> Tuple[str, Optional[int], Optional[int]]:
    """Parse a file reference string into (path, start_line, end_line).

    Formats:
    - path/to/file.py → (path, None, None)
    - path/to/file.py:42 → (path, 42, None)
    - path/to/file.py#L10-L20 → (path, 10, 20)
    - path/to/file.py:42-50 → (path, 42, 50)

    Returns (path, start_line, end_line) where lines are 1-indexed.
    Malformed refs return (ref, None, None) so we catch them during validation.
    """
    if not isinstance(ref, str) or not ref:
        return ref, None, None

    # Try #L{start}-L{end} format (GitHub style)
    match = re.match(r"^(.+?)#L(\d+)(?:-L(\d+))?$", ref)
    if match:
        path = match.group(1)
        start = int(match.group(2))
        end = int(match.group(3)) if match.group(3) else None
        return path, start, end

    # Try :{start}-{end} or :{start} format
    match = re.match(r"^(.+?):(\d+)(?:-(\d+))?$", ref)
    if match:
        path = match.group(1)
        start = int(match.group(2))
        end = int(match.group(3)) if match.group(3) else None
        return path, start, end

    # Plain path
    return ref, None, None


async def _file_exists_at_head(path: str) -> bool:
    """Check if a file exists in the HEAD commit using 'git ls-files'.

    Returns False if git command fails, file doesn't exist, or path is invalid.
    Does not raise exceptions — logs and returns False on errors.
    """
    if (
        not path
        or not isinstance(path, str)
        or "/" not in path
        and not path.endswith((".py", ".ts", ".tsx", ".js", ".go", ".rs"))
    ):
        # Reject obvious non-file paths to avoid wasting git calls
        return False

    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "ls-files",
            "--",
            path,
            cwd="/tmp/workspace",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning(
                f"[SOURCE_REF_VALIDATION] git ls-files timeout for path {path}"
            )
            return False
        # git ls-files returns 0 and outputs the path if it exists, empty otherwise
        return proc.returncode == 0 and path in stdout.decode()
    except Exception as e:
        logger.warning(
            f"[SOURCE_REF_VALIDATION] Failed to check if file exists: {path}: {e}"
        )
        return False


async def _file_has_line_count(path: str, min_line: int) -> bool:
    """Check if a file has at least min_line lines.

    Uses 'git show HEAD:path' and counts newlines to validate without checkout.
    Returns False if the file doesn't exist, has fewer lines, or git fails.
    Does not raise exceptions — logs and returns False on errors.
    """
    if not path or min_line < 1:
        return False

    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "show",
            f"HEAD:{path}",
            cwd="/tmp/workspace",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning(
                f"[SOURCE_REF_VALIDATION] Failed to validate line count for {path}"
            )
            return False

        if proc.returncode != 0:
            logger.warning(
                f"[SOURCE_REF_VALIDATION] Failed to count lines in {path}: {stderr.decode().strip()}"
            )
            return False

        line_count = stdout.count(b"\n")
        return line_count >= min_line
    except Exception as e:
        logger.warning(
            f"[SOURCE_REF_VALIDATION] Error validating line count for {path}: {e}"
        )
        return False


def extract_source_file_paths(sources: Optional[list]) -> list:
    """Extract normalized file paths from all file-type sources.

    Returns a deduplicated list of file paths (line numbers stripped) from
    file-type sources. Used by the reverse-index machinery to determine which
    source files a finding cites — e.g. to detect overlap with a PR's changed
    files during revalidation.
    """
    if not isinstance(sources, list):
        return []
    seen: set = set()
    paths = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        if source.get("type", "").lower() != "file":
            continue
        ref = source.get("ref")
        if not ref:
            continue
        path, _, _ = _parse_ref(ref)
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def summarize_broken_refs(
    validated_sources: Optional[list],
) -> Optional[Dict[str, Any]]:
    """Summarize file-type sources marked invalid by validate_source_refs.

    Walks the output of validate_source_refs and returns a structured
    provenance summary for findings whose citations include broken file refs:

        {"broken_source_ref_count": N, "broken_refs": [path, ...]}

    Paths are deduplicated using _parse_ref so different line numbers on the
    same file collapse to one entry. Returns None when there are no broken
    file refs (count would be 0). Non-file invalid sources are ignored.
    """
    if not isinstance(validated_sources, list):
        return None
    seen: set = set()
    broken_paths: list = []
    for source in validated_sources:
        if not isinstance(source, dict):
            continue
        if source.get("valid") is not False:
            continue
        if source.get("type", "").lower() != "file":
            continue
        ref = source.get("ref")
        if not ref:
            continue
        path, _, _ = _parse_ref(ref)
        if not path or path in seen:
            continue
        seen.add(path)
        broken_paths.append(path)
    if not broken_paths:
        return None
    return {
        "broken_source_ref_count": len(broken_paths),
        "broken_refs": broken_paths,
    }


def collect_stale_refs(validated_sources: list) -> list:
    """Return refs marked invalid by validate_source_refs.

    Given the output of validate_source_refs, returns the ref strings for
    sources marked valid=False. Callers use this to surface affected findings
    for re-evaluation when a file no longer exists at HEAD.
    """
    return [
        s["ref"]
        for s in validated_sources
        if isinstance(s, dict) and s.get("valid") is False and s.get("ref")
    ]


async def validate_source_refs(sources: Optional[list]) -> list:
    """Validate all file-type source refs in a sources list.

    For each source with type='file':
    - Validates that the file exists at HEAD
    - Validates that line numbers (if present) are within bounds
    - Marks invalid sources with valid: false or drops them

    For sources with type='pr' or type='message': no validation needed.

    Args:
        sources: List of source dicts with 'type' and 'ref' fields.
                Can be None.

    Returns:
        List of validated sources, with invalid file refs marked valid: false
        or dropped (depending on validation error severity).
    """
    if not isinstance(sources, list):
        return []

    validated: list[Dict[str, Any]] = []

    for source in sources:
        if not isinstance(source, dict):
            logger.warning("[SOURCE_REF_VALIDATION] Dropping non-dict source entry")
            continue

        source_type = source.get("type", "").lower()

        # PR and message sources don't need file validation
        if source_type in ("pr", "message"):
            validated.append(source)
            continue

        # File sources require validation
        if source_type == "file":
            ref = source.get("ref")
            if not ref:
                logger.warning(
                    "[SOURCE_REF_VALIDATION] File source missing 'ref' field"
                )
                continue

            path, start_line, end_line = _parse_ref(ref)

            # Validate path exists
            if not await _file_exists_at_head(path):
                logger.warning(
                    f"[SOURCE_REF_VALIDATION] File not found in HEAD: {path} "
                    f"(from ref {ref})"
                )
                # Mark as invalid rather than dropping, so downstream can decide
                # whether to use it or log it as stale
                marked = dict(source)
                marked["valid"] = False
                validated.append(marked)
                continue

            # Validate line numbers if present
            if start_line is not None:
                if not await _file_has_line_count(path, start_line):
                    logger.warning(
                        f"[SOURCE_REF_VALIDATION] Line number out of range: {path}:{start_line} "
                        f"(from ref {ref})"
                    )
                    marked = dict(source)
                    marked["valid"] = False
                    validated.append(marked)
                    continue

                # Also validate end_line if present
                if end_line is not None and end_line < start_line:
                    logger.warning(
                        f"[SOURCE_REF_VALIDATION] Invalid line range: {path}:{start_line}-{end_line} "
                        f"(end < start in ref {ref})"
                    )
                    marked = dict(source)
                    marked["valid"] = False
                    validated.append(marked)
                    continue

                if end_line is not None and not await _file_has_line_count(
                    path, end_line
                ):
                    logger.warning(
                        f"[SOURCE_REF_VALIDATION] End line out of range: {path}:{start_line}-{end_line} "
                        f"(from ref {ref})"
                    )
                    marked = dict(source)
                    marked["valid"] = False
                    validated.append(marked)
                    continue

            # Validation passed
            validated.append(source)
        else:
            # Unknown source type — pass through
            validated.append(source)

    return validated
