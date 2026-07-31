#!/usr/bin/env python3
"""Create one GCS object without requiring object read or list permission.

The gcloud storage CLI performs a post-upload object read, which is incompatible
with Blueprint's intentionally create-only backup writer. This helper obtains an
OAuth token from the already-configured gcloud identity and execs curl against
the JSON media-upload endpoint with an ifGenerationMatch=0 precondition.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,61}[a-z0-9]$")
_MAX_OBJECT_BYTES = 1024
_TOKEN_MAX_BYTES = 8192


def _destination(value: str) -> tuple[str, str]:
    if not value.startswith("gs://"):
        raise ValueError("destination must be a gs:// URI")
    bucket, separator, object_name = value[5:].partition("/")
    if not separator or not _BUCKET_RE.fullmatch(bucket):
        raise ValueError("destination bucket is invalid")
    encoded = object_name.encode("utf-8")
    if (
        not encoded
        or len(encoded) > _MAX_OBJECT_BYTES
        or any(ord(char) < 32 or ord(char) == 127 for char in object_name)
    ):
        raise ValueError("destination object name is invalid")
    return bucket, object_name


def _upload_url(destination: str) -> str:
    bucket, object_name = _destination(destination)
    query = urllib.parse.urlencode(
        {
            "uploadType": "media",
            "name": object_name,
            "ifGenerationMatch": "0",
        }
    )
    return f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o?{query}"


def _access_token() -> str:
    try:
        result = subprocess.run(
            ["gcloud", "auth", "print-access-token", "--quiet"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("could not obtain upload access token") from exc
    token = result.stdout.strip()
    if (
        result.returncode != 0
        or len(token.encode("utf-8")) > _TOKEN_MAX_BYTES
        or not token
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in token)
    ):
        raise RuntimeError("could not obtain upload access token")
    return token


def _curl_argv(*, source: str, destination: str, header_file: Path) -> list[str]:
    if source == "-":
        upload_source = "-"
    else:
        path = Path(source)
        if not path.is_absolute():
            raise ValueError("source must be '-' or an absolute path")
        upload_source = str(path)
    return [
        "curl",
        "--fail-with-body",
        "--silent",
        "--show-error",
        "--request",
        "POST",
        "--connect-timeout",
        "30",
        "--max-time",
        "3600",
        "--retry",
        "0",
        "--header",
        f"@{header_file}",
        "--upload-file",
        upload_source,
        "--output",
        "/dev/null",
        _upload_url(destination),
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        token = _access_token()
        descriptor, header_name = tempfile.mkstemp(prefix="gcs-upload-", suffix=".headers")
        header_file = Path(header_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as stream:
                stream.write(f"Authorization: Bearer {token}\n")
                stream.write("Content-Type: application/octet-stream\n")
                stream.flush()
                os.fsync(descriptor)
            command = _curl_argv(
                source=args.source,
                destination=args.destination,
                header_file=header_file,
            )
            os.execvpe(command[0], command, os.environ.copy())
        finally:
            os.close(descriptor)
            header_file.unlink(missing_ok=True)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"create-only GCS upload failed: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
