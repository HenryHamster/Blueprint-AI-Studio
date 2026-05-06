"""Validation of research finding source file references at ingestion time.

Validates that file refs in source objects actually exist and that line
numbers are within bounds. Refs that fail validation are marked with
valid: false or dropped, with warnings logged. The system degrades gracefully
on validation failures — does not crash.
"""
import logging
import re
import subprocess
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


def _file_exists_at_head(path: str) -> bool:
    """Check if a file exists in the HEAD commit using 'git ls-files'.

    Returns False if git command fails, file doesn't exist, or path is invalid.
    Does not raise exceptions — logs and returns False on errors.
    """
    if not path or not isinstance(path, str) or "/" not in path and not path.endswith((".py", ".ts", ".tsx", ".js", ".go", ".rs")):
        # Reject obvious non-file paths to avoid wasting git calls
        return False

    try:
        result = subprocess.run(
            ["git", "ls-files", "--", path],
            cwd="/tmp/workspace",
            capture_output=True,
            text=True,
            timeout=5,
        )
        # git ls-files returns 0 and outputs the path if it exists, empty otherwise
        return result.returncode == 0 and path in result.stdout
    except subprocess.TimeoutExpired:
        logger.warning(f"[SOURCE_REF_VALIDATION] git ls-files timeout for path {path}")
        return False
    except Exception as e:
        logger.warning(f"[SOURCE_REF_VALIDATION] Failed to check if file exists: {path}: {e}")
        return False


def _file_has_line_count(path: str, min_line: int) -> bool:
    """Check if a file has at least min_line lines.

    Uses 'git show HEAD:path | wc -l' to count lines without checking out.
    Returns False if the file doesn't exist, has fewer lines, or git fails.
    Does not raise exceptions — logs and returns False on errors.
    """
    if not path or min_line < 1:
        return False

    try:
        # Use git show to get file content, pipe to wc -l to count lines
        show_proc = subprocess.Popen(
            ["git", "show", f"HEAD:{path}"],
            cwd="/tmp/workspace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        wc_proc = subprocess.Popen(
            ["wc", "-l"],
            stdin=show_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        show_proc.stdout.close()  # Allow show_proc to receive SIGPIPE if wc exits
        stdout, stderr = wc_proc.communicate(timeout=5)
        show_proc.wait(timeout=5)

        if wc_proc.returncode != 0:
            logger.warning(
                f"[SOURCE_REF_VALIDATION] Failed to count lines in {path}: {stderr.strip()}"
            )
            return False

        line_count = int(stdout.strip().split()[0])
        return line_count >= min_line
    except (subprocess.TimeoutExpired, ValueError):
        logger.warning(f"[SOURCE_REF_VALIDATION] Failed to validate line count for {path}")
        return False
    except Exception as e:
        logger.warning(f"[SOURCE_REF_VALIDATION] Error validating line count for {path}: {e}")
        return False


def validate_source_refs(sources: Optional[list]) -> list:
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
            logger.warning(
                "[SOURCE_REF_VALIDATION] Dropping non-dict source entry"
            )
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
            if not _file_exists_at_head(path):
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
                if not _file_has_line_count(path, start_line):
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

                if end_line is not None and not _file_has_line_count(path, end_line):
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
