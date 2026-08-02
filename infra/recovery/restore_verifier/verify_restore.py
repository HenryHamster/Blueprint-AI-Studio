#!/usr/bin/env python3
"""Restore the newest recovery archive into an ephemeral private Cloud SQL drill."""

from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

_PROJECT_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_PROJECT_NUMBER_RE = re.compile(r"^[1-9][0-9]{5,20}$")
_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]$")
_REGION_RE = re.compile(r"^[a-z]+-[a-z0-9]+[0-9]$")
_NETWORK_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_SAFE_API_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_SAFE_PROVIDER_MESSAGE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9 .,;:_/@()+\-]{0,511}$"
)
_SENSITIVE_PROVIDER_MESSAGE_RE = re.compile(
    r"authorization|bearer|token|password|secret|credential|private[ _-]?key|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    re.IGNORECASE,
)
_HIGH_ENTROPY_PROVIDER_MESSAGE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"[A-Fa-f0-9]{32,}|"
    r"(?:sk|pk|rk)[_-][A-Za-z0-9_-]{16,}|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|"
    r"[A-Za-z0-9_+=-]{40,}"
    r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_REVISION_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")
_PREFIX_RE = re.compile(r"^[a-zA-Z0-9_./-]{1,160}$")
_INSTANCE_RE = re.compile(r"^[a-z][a-z0-9-]{0,96}[a-z0-9]$")
_OPERATION_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,200}$")
_ARCHIVE_OBJECT_RE = re.compile(r"^[a-zA-Z0-9_./-]{1,1024}$")
_PROXY_PORT = 5434
_DATABASE_NAME = "blueprint"
_DATABASE_USER = "postgres"
_DRILL_TIER = "db-custom-1-3840"
_DRILL_DISK_GB = 20
_COMPUTE_NETWORK_SELF_LINK_PREFIXES = (
    "https://compute.googleapis.com/compute/v1/",
    "https://www.googleapis.com/compute/v1/",
)
_REQUIRED_COLUMNS = {
    "alembic_version": {"version_num"},
    "alloweduser": {"id", "email", "role"},
    "project": {"id", "user_id", "status"},
    "task": {"id", "project_id", "status"},
    "artifact": {"id", "task_id", "artifact_type"},
}


class DrillError(RuntimeError):
    """A deliberately non-sensitive drill failure."""

    def __init__(
        self,
        stage: str,
        code: str,
        *,
        diagnostic: dict[str, Any] | None = None,
    ):
        super().__init__(f"{stage}:{code}")
        self.stage = stage
        self.code = code
        self.diagnostic = dict(diagnostic or {})


class AdminApiError(DrillError):
    """A non-sensitive Cloud SQL Admin API failure classification."""

    def __init__(
        self,
        stage: str,
        code: str,
        *,
        definitive_rejection: bool,
        diagnostic: dict[str, Any] | None = None,
    ):
        super().__init__(stage, code, diagnostic=diagnostic)
        self.definitive_rejection = definitive_rejection


def _required(env: dict[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise DrillError("configuration", f"missing_{name.lower()}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise DrillError("configuration", f"invalid_{name.lower()}")
    return value


def _network_resource_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    resource_path = value
    for prefix in _COMPUTE_NETWORK_SELF_LINK_PREFIXES:
        if value.startswith(prefix):
            resource_path = value.removeprefix(prefix)
            break
    parts = resource_path.split("/")
    if (
        len(parts) != 5
        or parts[0] != "projects"
        or not _PROJECT_RE.fullmatch(parts[1])
        or parts[2:4] != ["global", "networks"]
        or not _NETWORK_NAME_RE.fullmatch(parts[4])
    ):
        return None
    return resource_path


def _network_self_link(resource_path: str) -> str:
    return f"{_COMPUTE_NETWORK_SELF_LINK_PREFIXES[0]}{resource_path}"


def _safe_provider_message(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if (
        not _SAFE_PROVIDER_MESSAGE_RE.fullmatch(normalized)
        or _SENSITIVE_PROVIDER_MESSAGE_RE.search(normalized)
        or _HIGH_ENTROPY_PROVIDER_MESSAGE_RE.search(normalized)
    ):
        return None
    return normalized


def _bounded_int(
    env: dict[str, str], name: str, *, default: int, minimum: int, maximum: int
) -> int:
    raw = env.get(name, str(default))
    if not raw.isascii() or not raw.isdigit():
        raise DrillError("configuration", f"invalid_{name.lower()}")
    value = int(raw)
    if value < minimum or value > maximum:
        raise DrillError("configuration", f"invalid_{name.lower()}")
    return value


@dataclass(frozen=True)
class ArchivePin:
    name: str
    generation: str
    manifest_generation: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Config:
    recovery_project_id: str
    recovery_project_number: str
    source_project_id: str
    bucket_name: str
    region: str
    private_network: str
    private_service_range: str
    expected_revision: str
    archive_prefix: str
    evidence_prefix: str
    max_rpo_seconds: int
    instance_ttl_seconds: int
    operation_timeout_seconds: int
    proxy_start_timeout_seconds: int
    proxy_binary: str
    expected_archive: ArchivePin | None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        values = dict(os.environ if env is None else env)
        recovery_project = _required(values, "RECOVERY_PROJECT_ID")
        recovery_project_number = _required(values, "RECOVERY_PROJECT_NUMBER")
        source_project = _required(values, "SOURCE_PROJECT_ID")
        bucket = _required(values, "RECOVERY_BUCKET")
        region = _required(values, "RECOVERY_REGION")
        network = _required(values, "RECOVERY_PRIVATE_NETWORK")
        private_service_range = _required(values, "RECOVERY_PRIVATE_SERVICE_RANGE")
        revision = _required(values, "EXPECTED_ALEMBIC_REVISION")
        archive_prefix = values.get(
            "RECOVERY_ARCHIVE_PREFIX", "local-postgres-exports/"
        ).strip()
        evidence_prefix = values.get(
            "RECOVERY_EVIDENCE_PREFIX", "restore-drill-evidence/"
        ).strip()
        proxy_binary = values.get(
            "CLOUD_SQL_PROXY_BINARY", "/usr/local/bin/cloud-sql-proxy"
        ).strip()
        expected_archive_values = {
            "name": values.get("EXPECTED_ARCHIVE_NAME", "").strip(),
            "generation": values.get("EXPECTED_ARCHIVE_GENERATION", "").strip(),
            "manifest_generation": values.get(
                "EXPECTED_MANIFEST_GENERATION", ""
            ).strip(),
            "sha256": values.get("EXPECTED_ARCHIVE_SHA256", "").strip(),
            "size": values.get("EXPECTED_ARCHIVE_SIZE_BYTES", "").strip(),
        }

        if (
            not _PROJECT_RE.fullmatch(recovery_project)
            or not _PROJECT_RE.fullmatch(source_project)
            or recovery_project == source_project
        ):
            raise DrillError("configuration", "projects_not_independent")
        if not _PROJECT_NUMBER_RE.fullmatch(recovery_project_number):
            raise DrillError("configuration", "invalid_recovery_project_number")
        if not _BUCKET_RE.fullmatch(bucket):
            raise DrillError("configuration", "invalid_recovery_bucket")
        if not _REGION_RE.fullmatch(region):
            raise DrillError("configuration", "invalid_recovery_region")
        normalized_network = network.removeprefix("/")
        expected_network_prefix = (
            f"projects/{recovery_project}/global/networks/"
        )
        network_name = normalized_network.removeprefix(expected_network_prefix)
        if (
            not normalized_network.startswith(expected_network_prefix)
            or not _NETWORK_NAME_RE.fullmatch(network_name)
        ):
            raise DrillError("configuration", "network_not_in_recovery_project")
        if not _NETWORK_NAME_RE.fullmatch(private_service_range):
            raise DrillError("configuration", "invalid_private_service_range")
        if not _REVISION_RE.fullmatch(revision):
            raise DrillError("configuration", "invalid_expected_revision")
        if (
            not _PREFIX_RE.fullmatch(archive_prefix)
            or not archive_prefix.endswith("/")
            or not _PREFIX_RE.fullmatch(evidence_prefix)
            or not evidence_prefix.endswith("/")
        ):
            raise DrillError("configuration", "invalid_storage_prefix")
        if not Path(proxy_binary).is_absolute():
            raise DrillError("configuration", "proxy_binary_not_absolute")
        expected_archive: ArchivePin | None = None
        if any(expected_archive_values.values()):
            if not all(expected_archive_values.values()):
                raise DrillError("configuration", "incomplete_expected_archive")
            expected_name = expected_archive_values["name"]
            if (
                not _ARCHIVE_OBJECT_RE.fullmatch(expected_name)
                or ".." in expected_name.split("/")
                or not expected_name.startswith(archive_prefix)
                or not expected_name.endswith(".sql.gz")
            ):
                raise DrillError("configuration", "invalid_expected_archive_name")
            if (
                not expected_archive_values["generation"].isdigit()
                or not expected_archive_values["manifest_generation"].isdigit()
                or not re.fullmatch(
                    r"[0-9a-f]{64}", expected_archive_values["sha256"]
                )
                or not expected_archive_values["size"].isdigit()
                or int(expected_archive_values["size"]) <= 0
            ):
                raise DrillError("configuration", "invalid_expected_archive")
            expected_archive = ArchivePin(
                name=expected_name,
                generation=expected_archive_values["generation"],
                manifest_generation=expected_archive_values[
                    "manifest_generation"
                ],
                sha256=expected_archive_values["sha256"],
                size=int(expected_archive_values["size"]),
            )

        return cls(
            recovery_project_id=recovery_project,
            recovery_project_number=recovery_project_number,
            source_project_id=source_project,
            bucket_name=bucket,
            region=region,
            private_network=normalized_network,
            private_service_range=private_service_range,
            expected_revision=revision,
            archive_prefix=archive_prefix,
            evidence_prefix=evidence_prefix,
            max_rpo_seconds=3600
            * _bounded_int(
                values, "MAX_RPO_HOURS", default=24, minimum=1, maximum=168
            ),
            instance_ttl_seconds=60
            * _bounded_int(
                values,
                "DRILL_INSTANCE_TTL_MINUTES",
                default=180,
                minimum=30,
                maximum=1440,
            ),
            operation_timeout_seconds=_bounded_int(
                values,
                "DRILL_OPERATION_TIMEOUT_SECONDS",
                default=3600,
                minimum=300,
                maximum=7200,
            ),
            proxy_start_timeout_seconds=_bounded_int(
                values,
                "DRILL_PROXY_START_TIMEOUT_SECONDS",
                default=60,
                minimum=5,
                maximum=120,
            ),
            proxy_binary=proxy_binary,
            expected_archive=expected_archive,
        )


@dataclass(frozen=True)
class Archive:
    bucket: str
    name: str
    generation: str
    created_at: datetime
    size: int
    sha256: str
    manifest_generation: str

    @property
    def uri(self) -> str:
        return f"gs://{self.bucket}/{self.name}"

    @property
    def archive_id(self) -> str:
        return f"{self.bucket}/{self.name}#{self.generation}"


class RecoveryStorage:
    def __init__(self, client: Any, config: Config):
        self._client = client
        self._config = config
        self._bucket_verified = False

    def _bucket(self) -> Any:
        bucket = self._client.bucket(self._config.bucket_name)
        if not self._bucket_verified:
            try:
                bucket.reload(timeout=60)
            except Exception as exc:
                raise DrillError(
                    "storage_scope", "bucket_metadata_unavailable"
                ) from exc
            project_number = str(getattr(bucket, "project_number", "") or "")
            if project_number != self._config.recovery_project_number:
                raise DrillError("storage_scope", "bucket_not_in_recovery_project")
            self._bucket_verified = True
        return bucket

    @staticmethod
    def _created(blob: Any) -> datetime | None:
        created = getattr(blob, "time_created", None) or getattr(blob, "updated", None)
        if created is None:
            return None
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return created.astimezone(timezone.utc)

    def _archive_from_manifest(
        self, manifest_blob: Any, expected: ArchivePin | None = None
    ) -> Archive | None:
        name = str(getattr(manifest_blob, "name", "") or "")
        if not name.endswith(".sql.gz.manifest.json"):
            return None
        if int(getattr(manifest_blob, "size", 0) or 0) <= 0 or int(
            getattr(manifest_blob, "size", 0) or 0
        ) > 65536:
            return None
        try:
            payload = json.loads(manifest_blob.download_as_text(timeout=60))
        except Exception:
            return None
        manifest_generation = str(getattr(manifest_blob, "generation", "") or "")
        archive_name = name.removesuffix(".manifest.json")
        expected_uri = f"gs://{self._config.bucket_name}/{archive_name}"
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("archive_name") != archive_name
            or payload.get("archive_uri") != expected_uri
            or payload.get("format") != "plain-sql-gzip"
            or payload.get("database") != _DATABASE_NAME
            or payload.get("postgres_major") != 15
            or not re.fullmatch(r"[0-9a-f]{32}", str(payload.get("archive_id", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("sha256", "")))
            or not isinstance(payload.get("size_bytes"), int)
            or payload["size_bytes"] <= 0
            or (
                expected is not None
                and (
                    archive_name != expected.name
                    or manifest_generation != expected.manifest_generation
                    or payload["sha256"] != expected.sha256
                    or payload["size_bytes"] != expected.size
                )
            )
        ):
            return None
        bucket = self._bucket()
        archive_blob = bucket.blob(
            archive_name,
            generation=(int(expected.generation) if expected is not None else None),
        )
        try:
            archive_blob.reload(timeout=60)
        except Exception:
            return None
        generation = str(getattr(archive_blob, "generation", "") or "")
        created = self._created(archive_blob)
        if (
            not generation.isdigit()
            or not manifest_generation.isdigit()
            or created is None
            or int(getattr(archive_blob, "size", 0) or 0) != payload["size_bytes"]
            or (expected is not None and generation != expected.generation)
        ):
            return None
        digest = hashlib.sha256()
        observed_size = 0
        try:
            with archive_blob.open("rb") as stream:
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    observed_size += len(chunk)
                    digest.update(chunk)
        except Exception:
            return None
        if (
            observed_size != payload["size_bytes"]
            or digest.hexdigest() != payload["sha256"]
        ):
            return None
        return Archive(
            bucket=self._config.bucket_name,
            name=archive_name,
            generation=generation,
            created_at=created,
            size=observed_size,
            sha256=digest.hexdigest(),
            manifest_generation=manifest_generation,
        )

    def latest_archive(self, now: datetime) -> Archive:
        bucket = self._bucket()
        expected = self._config.expected_archive
        if expected is not None:
            manifest_blob = bucket.blob(
                f"{expected.name}.manifest.json",
                generation=int(expected.manifest_generation),
            )
            try:
                manifest_blob.reload(timeout=60)
            except Exception as exc:
                raise DrillError(
                    "archive_selection", "expected_archive_not_found"
                ) from exc
            archive = self._archive_from_manifest(manifest_blob, expected)
            if archive is None:
                raise DrillError("archive_selection", "expected_archive_invalid")
        else:
            manifests = [
                blob
                for blob in self._client.list_blobs(
                    self._config.bucket_name,
                    prefix=self._config.archive_prefix,
                    timeout=60,
                )
                if str(getattr(blob, "name", "") or "").endswith(
                    ".sql.gz.manifest.json"
                )
            ]
            manifests.sort(
                key=lambda blob: (
                    self._created(blob)
                    or datetime.min.replace(tzinfo=timezone.utc),
                    str(getattr(blob, "name", "") or ""),
                ),
                reverse=True,
            )
            archive = (
                self._archive_from_manifest(manifests[0]) if manifests else None
            )
            if archive is None:
                error_code = (
                    "newest_archive_invalid" if manifests else "no_usable_archive"
                )
                raise DrillError("archive_selection", error_code)
        rpo_seconds = (now - archive.created_at).total_seconds()
        if rpo_seconds < -300:
            raise DrillError("archive_selection", "archive_timestamp_in_future")
        if rpo_seconds > self._config.max_rpo_seconds:
            raise DrillError("archive_selection", "archive_exceeds_rpo")
        return archive

    def assert_import_generation_current(self, archive: Archive) -> None:
        current = self._bucket().blob(archive.name)
        try:
            current.reload(timeout=60)
        except Exception as exc:
            raise DrillError(
                "archive_import", "archive_current_generation_unavailable"
            ) from exc
        if (
            str(getattr(current, "generation", "") or "") != archive.generation
            or int(getattr(current, "size", 0) or 0) != archive.size
        ):
            raise DrillError("archive_import", "archive_generation_changed")

    def write_evidence(self, object_name: str, payload: dict[str, Any]) -> None:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        blob = self._bucket().blob(object_name)
        blob.upload_from_string(
            encoded,
            content_type="application/json",
            if_generation_match=0,
            timeout=60,
        )


class RecoveryLogger:
    def __init__(self, client: Any):
        self._logger = client.logger("blueprint-recovery-restore-drill")

    def write(self, payload: dict[str, Any]) -> None:
        severity = "INFO" if payload["outcome"] == "success" else "ERROR"
        self._logger.log_struct(payload, severity=severity)


class SqlAdminRest:
    """Small, recovery-project-scoped Cloud SQL Admin REST client."""

    _BASE = "https://sqladmin.googleapis.com/v1"

    def __init__(
        self,
        session: Any,
        project_id: str,
        *,
        request_timeout: tuple[int, int] = (10, 60),
    ):
        if not _PROJECT_RE.fullmatch(project_id):
            raise DrillError("configuration", "invalid_recovery_project")
        self._session = session
        self.project_id = project_id
        self._request_timeout = request_timeout

    @staticmethod
    def _part(value: str, pattern: re.Pattern[str]) -> str:
        if not pattern.fullmatch(value):
            raise DrillError("cloud_sql_api", "unsafe_resource_name")
        return quote(value, safe="")

    def _request(
        self,
        method: str,
        path: str,
        *,
        stage: str,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        allow_not_found: bool = False,
    ) -> dict[str, Any] | None:
        try:
            response = self._session.request(
                method,
                f"{self._BASE}{path}",
                json=body,
                params=params,
                timeout=self._request_timeout,
            )
        except Exception as exc:
            raise AdminApiError(
                stage,
                "admin_api_transport_failed",
                definitive_rejection=False,
            ) from exc
        if allow_not_found and response.status_code == 404:
            return None
        if response.status_code < 200 or response.status_code >= 300:
            status = int(response.status_code)
            codes = {
                400: "admin_api_bad_request",
                401: "admin_api_unauthorized",
                403: "admin_api_forbidden",
                404: "admin_api_not_found",
                409: "admin_api_conflict",
                429: "admin_api_rate_limited",
            }
            code = codes.get(
                status,
                "admin_api_unavailable" if status >= 500 else "admin_api_request_failed",
            )
            diagnostic: dict[str, Any] = {"http_status": status}
            try:
                error_payload = response.json()
            except (TypeError, ValueError):
                error_payload = None
            if isinstance(error_payload, dict):
                error = error_payload.get("error")
                if isinstance(error, dict):
                    provider_status = error.get("status")
                    errors = error.get("errors")
                    provider_reason = (
                        errors[0].get("reason")
                        if isinstance(errors, list)
                        and errors
                        and isinstance(errors[0], dict)
                        else None
                    )
                    for key, value in (
                        ("provider_status", provider_status),
                        ("provider_reason", provider_reason),
                    ):
                        if isinstance(value, str) and _SAFE_API_TOKEN_RE.fullmatch(value):
                            diagnostic[key] = re.sub(
                                r"(?<=[a-z0-9])(?=[A-Z])", "_", value
                            ).replace("-", "_").upper()
                    if stage == "instance_create" and status in {400, 422}:
                        nested_message = (
                            errors[0].get("message")
                            if isinstance(errors, list)
                            and errors
                            and isinstance(errors[0], dict)
                            else None
                        )
                        provider_message = _safe_provider_message(
                            nested_message or error.get("message")
                        )
                        if provider_message is None:
                            diagnostic["provider_message_redacted"] = True
                        else:
                            diagnostic["provider_message"] = provider_message
            raise AdminApiError(
                stage,
                code,
                definitive_rejection=status in {400, 401, 403, 404, 409, 422},
                diagnostic=diagnostic,
            )
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise DrillError(stage, "admin_api_invalid_json") from exc
        if not isinstance(payload, dict):
            raise DrillError(stage, "admin_api_invalid_json")
        return payload

    def create_instance(self, name: str, body: dict[str, Any]) -> str:
        self._part(name, _INSTANCE_RE)
        payload = self._request(
            "POST",
            f"/projects/{self.project_id}/instances",
            stage="instance_create",
            body=body,
        )
        return self._operation_name(payload, "instance_create")

    def list_instances(self) -> list[dict[str, Any]]:
        instances: list[dict[str, Any]] = []
        page_token: str | None = None
        for _ in range(100):
            params = {"maxResults": "100"}
            if page_token is not None:
                params["pageToken"] = page_token
            payload = self._request(
                "GET",
                f"/projects/{self.project_id}/instances",
                stage="janitor",
                params=params,
            ) or {}
            page = payload.get("items") or []
            if not isinstance(page, list) or any(
                not isinstance(item, dict) for item in page
            ):
                raise DrillError("janitor", "instance_list_invalid")
            instances.extend(page)
            token = payload.get("nextPageToken")
            if token is None:
                return instances
            if (
                not isinstance(token, str)
                or not token
                or len(token) > 2048
                or any(ord(char) < 32 or ord(char) == 127 for char in token)
            ):
                raise DrillError("janitor", "page_token_invalid")
            page_token = token
        raise DrillError("janitor", "too_many_instance_pages")

    def get_instance(self, name: str) -> dict[str, Any]:
        name = self._part(name, _INSTANCE_RE)
        return self._request(
            "GET",
            f"/projects/{self.project_id}/instances/{name}",
            stage="instance_verify",
        ) or {}

    def get_instance_if_exists(self, name: str) -> dict[str, Any] | None:
        name = self._part(name, _INSTANCE_RE)
        return self._request(
            "GET",
            f"/projects/{self.project_id}/instances/{name}",
            stage="cleanup_reconcile",
            allow_not_found=True,
        )

    def create_database(self, instance: str, database: str) -> str:
        instance = self._part(instance, _INSTANCE_RE)
        if database != _DATABASE_NAME:
            raise DrillError("database_create", "unexpected_database_name")
        payload = self._request(
            "POST",
            f"/projects/{self.project_id}/instances/{instance}/databases",
            stage="database_create",
            body={"name": database},
        )
        return self._operation_name(payload, "database_create")

    def set_postgres_password(self, instance: str, password: str) -> str:
        instance = self._part(instance, _INSTANCE_RE)
        payload = self._request(
            "PUT",
            f"/projects/{self.project_id}/instances/{instance}/users",
            stage="database_user",
            params={"host": "", "name": _DATABASE_USER},
            body={"password": password},
        )
        return self._operation_name(payload, "database_user")

    def import_archive(self, instance: str, archive_uri: str) -> str:
        instance = self._part(instance, _INSTANCE_RE)
        payload = self._request(
            "POST",
            f"/projects/{self.project_id}/instances/{instance}/import",
            stage="archive_import",
            body={
                "importContext": {
                    "kind": "sql#importContext",
                    "fileType": "SQL",
                    "uri": archive_uri,
                    "database": _DATABASE_NAME,
                }
            },
        )
        return self._operation_name(payload, "archive_import")

    def get_operation(self, operation: str, *, stage: str) -> dict[str, Any]:
        operation = self._part(operation, _OPERATION_RE)
        return self._request(
            "GET",
            f"/projects/{self.project_id}/operations/{operation}",
            stage=stage,
        ) or {}

    def delete_instance(self, name: str) -> str | None:
        name = self._part(name, _INSTANCE_RE)
        payload = self._request(
            "DELETE",
            f"/projects/{self.project_id}/instances/{name}",
            stage="cleanup",
            allow_not_found=True,
        )
        if payload is None:
            return None
        return self._operation_name(payload, "cleanup")

    @staticmethod
    def _operation_name(payload: dict[str, Any] | None, stage: str) -> str:
        name = payload.get("name") if isinstance(payload, dict) else None
        if not isinstance(name, str) or not _OPERATION_RE.fullmatch(name):
            raise DrillError(stage, "operation_name_missing")
        return name


@dataclass
class Runtime:
    storage: RecoveryStorage
    logger: RecoveryLogger
    sql: SqlAdminRest
    db_connect: Callable[..., Any]
    popen: Callable[..., Any] = subprocess.Popen
    create_connection: Callable[..., Any] = socket.create_connection
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    password: Callable[[int], str] = secrets.token_urlsafe
    suffix: Callable[[int], str] = secrets.token_hex


def _instance_name(now: datetime, suffix: str) -> str:
    suffix = suffix.lower()
    if not re.fullmatch(r"[0-9a-f]{6}", suffix):
        raise DrillError("configuration", "invalid_instance_suffix")
    name = f"bp-restore-{now.strftime('%Y%m%d%H%M')}-{suffix}"
    if not _INSTANCE_RE.fullmatch(name):
        raise DrillError("configuration", "invalid_instance_name")
    return name


def _instance_body(config: Config, name: str, expires_at: datetime) -> dict[str, Any]:
    return {
        "name": name,
        "region": config.region,
        "databaseVersion": "POSTGRES_15",
        "sqlNetworkArchitecture": "NEW_NETWORK_ARCHITECTURE",
        "settings": {
            "tier": _DRILL_TIER,
            "availabilityType": "ZONAL",
            "activationPolicy": "ALWAYS",
            "dataDiskType": "PD_SSD",
            "dataDiskSizeGb": str(_DRILL_DISK_GB),
            "storageAutoResize": False,
            "deletionProtectionEnabled": False,
            "backupConfiguration": {"enabled": False},
            "ipConfiguration": {
                "ipv4Enabled": False,
                "privateNetwork": _network_self_link(config.private_network),
                "allocatedIpRange": config.private_service_range,
                "authorizedNetworks": [],
            },
            "userLabels": {
                "blueprint-purpose": "restore-drill",
                "blueprint-drill": name.removeprefix("bp-restore-"),
                "expires-epoch": str(int(expires_at.timestamp())),
            },
        },
    }


def _is_owned_cleanup_target(
    instance: dict[str, Any] | None,
    config: Config,
    name: str,
    expires_at: datetime,
) -> bool:
    if not isinstance(instance, dict):
        return False
    settings = instance.get("settings")
    labels = settings.get("userLabels") if isinstance(settings, dict) else None
    return (
        instance.get("project") == config.recovery_project_id
        and instance.get("name") == name
        and isinstance(labels, dict)
        and labels.get("blueprint-purpose") == "restore-drill"
        and labels.get("blueprint-drill") == name.removeprefix("bp-restore-")
        and labels.get("expires-epoch") == str(int(expires_at.timestamp()))
    )


def _reconcile_drill_instances(
    runtime: Runtime, config: Config, now: datetime
) -> list[str]:
    """Delete expired or ambiguous instances that are unambiguously drill-owned."""

    removed: list[str] = []
    now_epoch = int(now.timestamp())
    maximum_future = now_epoch + config.instance_ttl_seconds + 300
    for instance in runtime.sql.list_instances():
        name = instance.get("name")
        settings = instance.get("settings") or {}
        labels = settings.get("userLabels") or {}
        if (
            not isinstance(name, str)
            or not name.startswith("bp-restore-")
            or not _INSTANCE_RE.fullmatch(name)
            or labels.get("blueprint-purpose") != "restore-drill"
        ):
            continue
        raw_expiry = labels.get("expires-epoch")
        expiry = (
            int(raw_expiry)
            if isinstance(raw_expiry, str) and raw_expiry.isascii() and raw_expiry.isdigit()
            else None
        )
        ambiguous = expiry is None or expiry > maximum_future
        expired = expiry is not None and expiry <= now_epoch
        if not (ambiguous or expired):
            continue
        operation = runtime.sql.delete_instance(name)
        if operation:
            _wait_operation(runtime, config, operation, stage="janitor")
        removed.append(name)
    return removed


def _wait_operation(
    runtime: Runtime,
    config: Config,
    operation: str,
    *,
    stage: str,
) -> dict[str, Any]:
    deadline = runtime.monotonic() + config.operation_timeout_seconds
    while runtime.monotonic() < deadline:
        payload = runtime.sql.get_operation(operation, stage=stage)
        status = payload.get("status")
        if status == "DONE":
            if payload.get("error"):
                raise DrillError(stage, "operation_failed")
            return payload
        if status not in {"PENDING", "RUNNING"}:
            raise DrillError(stage, "unexpected_operation_status")
        runtime.sleep(5)
    raise DrillError(stage, "operation_timed_out")


def _verify_private_instance(instance: dict[str, Any], config: Config) -> str:
    settings = instance.get("settings") or {}
    ip_config = settings.get("ipConfiguration") or {}
    if (
        instance.get("project") not in {None, config.recovery_project_id}
        or instance.get("databaseVersion") != "POSTGRES_15"
        or instance.get("sqlNetworkArchitecture")
        != "NEW_NETWORK_ARCHITECTURE"
        or ip_config.get("ipv4Enabled") is not False
        or _network_resource_path(ip_config.get("privateNetwork"))
        != config.private_network
        or ip_config.get("allocatedIpRange")
        != config.private_service_range
    ):
        raise DrillError("instance_verify", "instance_not_private_recovery_only")
    for address in instance.get("ipAddresses") or []:
        if address.get("type") in {"PRIMARY", "OUTGOING"}:
            raise DrillError("instance_verify", "public_ip_present")
    connection_name = instance.get("connectionName")
    expected_prefix = f"{config.recovery_project_id}:{config.region}:"
    if (
        not isinstance(connection_name, str)
        or not connection_name.startswith(expected_prefix)
    ):
        raise DrillError("instance_verify", "invalid_connection_name")
    return connection_name


def _start_proxy(
    runtime: Runtime, config: Config, connection_name: str
) -> tuple[Any, list[str]]:
    command = [
        config.proxy_binary,
        "--address=127.0.0.1",
        f"--port={_PROXY_PORT}",
        "--private-ip",
        connection_name,
    ]
    process = runtime.popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    try:
        deadline = runtime.monotonic() + config.proxy_start_timeout_seconds
        while runtime.monotonic() < deadline:
            if process.poll() is not None:
                raise DrillError("proxy_start", "proxy_exited")
            try:
                connection = runtime.create_connection(
                    ("127.0.0.1", _PROXY_PORT), timeout=1
                )
            except OSError:
                runtime.sleep(0.5)
                continue
            connection.close()
            return process, command
        raise DrillError("proxy_start", "proxy_timed_out")
    except Exception:
        _stop_proxy(process)
        raise


def _stop_proxy(process: Any | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _database_checks(
    runtime: Runtime, config: Config, password: str
) -> tuple[list[dict[str, Any]], str]:
    checks: list[dict[str, Any]] = []
    with runtime.db_connect(
        host="127.0.0.1",
        port=_PROXY_PORT,
        dbname=_DATABASE_NAME,
        user=_DATABASE_USER,
        password=password,
        connect_timeout=10,
        sslmode="disable",
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = '30000ms'")
            cursor.execute("SELECT 1")
            if cursor.fetchone() != (1,):
                raise DrillError("database_verify", "app_health_query_failed")
            checks.append({"name": "app_health_query", "passed": True})

            cursor.execute("SELECT current_setting('server_version_num')::int")
            server_version = int(cursor.fetchone()[0])
            if server_version < 150000 or server_version >= 160000:
                raise DrillError("database_verify", "unexpected_postgres_version")
            checks.append(
                {
                    "name": "postgres_15",
                    "passed": True,
                    "server_version_num": server_version,
                }
            )

            cursor.execute("SELECT version_num FROM alembic_version LIMIT 2")
            revision_rows = cursor.fetchall()
            if len(revision_rows) != 1:
                raise DrillError("database_verify", "invalid_revision_rows")
            observed_revision = str(revision_rows[0][0])
            if observed_revision != config.expected_revision:
                raise DrillError("database_verify", "schema_revision_mismatch")
            checks.append(
                {
                    "name": "alembic_revision",
                    "passed": True,
                    "observed": observed_revision,
                    "expected": config.expected_revision,
                }
            )

            cursor.execute(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = ANY(%s)
                """,
                (list(_REQUIRED_COLUMNS),),
            )
            present: dict[str, set[str]] = {}
            for table, column in cursor.fetchall():
                present.setdefault(str(table), set()).add(str(column))
            missing = {
                table: sorted(columns - present.get(table, set()))
                for table, columns in _REQUIRED_COLUMNS.items()
                if columns - present.get(table, set())
            }
            if missing:
                raise DrillError("database_verify", "required_schema_missing")
            checks.append(
                {
                    "name": "app_schema_compatible",
                    "passed": True,
                    "relations_checked": len(_REQUIRED_COLUMNS),
                }
            )

            counts: dict[str, int] = {}
            for table in ("alloweduser", "project", "task", "artifact"):
                cursor.execute(f'SELECT count(*) FROM "{table}"')
                counts[table] = int(cursor.fetchone()[0])
            if counts["task"] > 0 and counts["project"] == 0:
                raise DrillError("database_verify", "aggregate_integrity_failed")
            checks.append(
                {
                    "name": "non_sensitive_row_counts",
                    "passed": True,
                    "counts": counts,
                }
            )

            cursor.execute(
                """
                SELECT count(*)
                FROM task AS t
                LEFT JOIN project AS p ON p.id = t.project_id
                WHERE p.id IS NULL
                """
            )
            orphan_tasks = int(cursor.fetchone()[0])
            cursor.execute(
                """
                SELECT count(*)
                FROM artifact AS a
                LEFT JOIN task AS t ON t.id = a.task_id
                WHERE t.id IS NULL
                """
            )
            orphan_artifacts = int(cursor.fetchone()[0])
            cursor.execute(
                "SELECT count(*) FROM pg_constraint WHERE NOT convalidated"
            )
            unvalidated_constraints = int(cursor.fetchone()[0])
            if orphan_tasks or orphan_artifacts or unvalidated_constraints:
                raise DrillError("database_verify", "relational_integrity_failed")
            checks.append(
                {
                    "name": "relational_integrity",
                    "passed": True,
                    "orphan_tasks": orphan_tasks,
                    "orphan_artifacts": orphan_artifacts,
                    "unvalidated_constraints": unvalidated_constraints,
                }
            )
    return checks, observed_revision


def _safe_evidence_name(
    config: Config, started_at: datetime, drill_id: str
) -> str:
    return (
        f"{config.evidence_prefix}{started_at.strftime('%Y/%m/%d')}/"
        f"{drill_id}.json"
    )


def run_drill(config: Config, runtime: Runtime) -> tuple[int, dict[str, Any]]:
    started_at = runtime.now().astimezone(timezone.utc)
    drill_id = _instance_name(started_at, runtime.suffix(3))
    expires_at = datetime.fromtimestamp(
        started_at.timestamp() + config.instance_ttl_seconds, tz=timezone.utc
    )
    evidence: dict[str, Any] = {
        "event": "db_restore_drill",
        "drill_id": drill_id,
        "recovery_project_id": config.recovery_project_id,
        "source_project_id": config.source_project_id,
        "started_at": started_at.isoformat(),
        "expected_revision": config.expected_revision,
        "instance_expiry": expires_at.isoformat(),
        "outcome": "failure",
        "stage": "janitor",
        "checks": [],
        "cleanup": {
            "intent_recorded": False,
            "attempted": False,
            "completed": False,
        },
    }
    archive: Archive | None = None
    instance_request_state = "not_requested"
    proxy = None
    password = ""
    failure: DrillError | None = None
    restore_verified_at: datetime | None = None

    try:
        evidence["janitor_removed"] = _reconcile_drill_instances(
            runtime, config, started_at
        )
        evidence["stage"] = "archive_selection"
        archive = runtime.storage.latest_archive(started_at)
        rpo_seconds = max(0, int((started_at - archive.created_at).total_seconds()))
        evidence["archive"] = {
            "id": archive.archive_id,
            "created_at": archive.created_at.isoformat(),
            "manifest_generation": archive.manifest_generation,
            "sha256": archive.sha256,
            "size_bytes": archive.size,
            "rpo_seconds": rpo_seconds,
            "selection": (
                "expected" if config.expected_archive is not None else "newest_usable"
            ),
        }

        evidence["stage"] = "instance_create"
        # Record cleanup intent before the create request: an ambiguous API
        # response can still mean the instance was created server-side.
        instance_request_state = "ambiguous"
        evidence["cleanup"]["intent_recorded"] = True
        try:
            operation = runtime.sql.create_instance(
                drill_id, _instance_body(config, drill_id, expires_at)
            )
            instance_request_state = "accepted"
        except AdminApiError as exc:
            if exc.definitive_rejection:
                instance_request_state = "rejected"
            raise
        _wait_operation(runtime, config, operation, stage="instance_create")

        evidence["stage"] = "instance_verify"
        connection_name = _verify_private_instance(
            runtime.sql.get_instance(drill_id), config
        )

        evidence["stage"] = "database_create"
        operation = runtime.sql.create_database(drill_id, _DATABASE_NAME)
        _wait_operation(runtime, config, operation, stage="database_create")

        evidence["stage"] = "database_user"
        password = runtime.password(48)
        if len(password) < 48:
            raise DrillError("database_user", "password_generation_failed")
        operation = runtime.sql.set_postgres_password(drill_id, password)
        _wait_operation(runtime, config, operation, stage="database_user")

        evidence["stage"] = "archive_import"
        runtime.storage.assert_import_generation_current(archive)
        operation = runtime.sql.import_archive(drill_id, archive.uri)
        _wait_operation(runtime, config, operation, stage="archive_import")
        runtime.storage.assert_import_generation_current(archive)
        evidence["archive"]["generation_stable_during_import"] = True

        evidence["stage"] = "proxy_start"
        proxy, _proxy_command = _start_proxy(runtime, config, connection_name)

        evidence["stage"] = "database_verify"
        checks, revision = _database_checks(runtime, config, password)
        checks.insert(
            0,
            {
                "name": "archive_decryption_and_import",
                "passed": True,
            },
        )
        restore_verified_at = runtime.now().astimezone(timezone.utc)
        evidence.update(
            {
                "outcome": "success",
                "stage": "verified",
                "checks": checks,
                "observed_revision": revision,
                "rto_seconds": max(
                    0, int((restore_verified_at - started_at).total_seconds())
                ),
                "verified_at": restore_verified_at.isoformat(),
            }
        )
    except DrillError as exc:
        failure = exc
    except Exception:
        failure = DrillError(evidence["stage"], "unexpected_error")
    finally:
        password = ""
        try:
            _stop_proxy(proxy)
        except Exception:
            proxy_cleanup_failure = DrillError(
                "proxy_cleanup", "proxy_cleanup_failed"
            )
            if failure is None:
                failure = proxy_cleanup_failure
            evidence["proxy_cleanup"] = {
                "completed": False,
                "failure_code": proxy_cleanup_failure.code,
            }
        if instance_request_state == "rejected":
            evidence["cleanup"].update({"completed": True, "not_required": True})
        elif instance_request_state in {"ambiguous", "accepted"}:
            cleanup_target_confirmed = instance_request_state == "accepted"
            if instance_request_state == "ambiguous":
                evidence["cleanup"]["ownership_check_attempted"] = True
                try:
                    cleanup_target_confirmed = _is_owned_cleanup_target(
                        runtime.sql.get_instance_if_exists(drill_id),
                        config,
                        drill_id,
                        expires_at,
                    )
                    evidence["cleanup"]["ownership_check_completed"] = True
                except Exception:
                    cleanup_target_confirmed = False
                    evidence["cleanup"]["ownership_check_completed"] = False
                    evidence["cleanup"][
                        "ownership_check_failure_code"
                    ] = "cleanup_reconcile_failed"
                if not cleanup_target_confirmed:
                    evidence["cleanup"]["deferred_to_janitor"] = True
            if not cleanup_target_confirmed:
                evidence["cleanup"]["completed"] = False
            else:
                evidence["cleanup"]["attempted"] = True
                try:
                    delete_operation = runtime.sql.delete_instance(drill_id)
                    if delete_operation:
                        _wait_operation(
                            runtime, config, delete_operation, stage="cleanup"
                        )
                    evidence["cleanup"]["completed"] = True
                except Exception:
                    cleanup_failure = DrillError("cleanup", "instance_cleanup_failed")
                    evidence["cleanup"]["failure_code"] = cleanup_failure.code
                    if failure is None:
                        failure = cleanup_failure
                    evidence["outcome"] = "failure"
                    evidence["stage"] = cleanup_failure.stage
                    evidence["failure_code"] = cleanup_failure.code

    if failure is not None:
        evidence["outcome"] = "failure"
        evidence["stage"] = failure.stage
        evidence["failure_code"] = failure.code
        if failure.diagnostic:
            evidence["failure_diagnostic"] = failure.diagnostic
        evidence.pop("observed_revision", None)
    evidence["finished_at"] = runtime.now().astimezone(timezone.utc).isoformat()
    object_name = _safe_evidence_name(config, started_at, drill_id)
    evidence["evidence_object"] = f"gs://{config.bucket_name}/{object_name}"

    storage_written = False
    evidence["evidence_write"] = {"completed": True}
    try:
        runtime.storage.write_evidence(object_name, evidence)
        storage_written = True
    except Exception:
        evidence["evidence_write"] = {
            "completed": False,
            "failure_code": "evidence_bucket_write_failed",
        }
        # Preserve the causal failure when the drill had already failed. A
        # secondary evidence-channel failure must not erase the only useful
        # diagnosis. Evidence persistence remains a top-level failure when the
        # restore itself otherwise succeeded.
        if evidence["outcome"] == "success":
            evidence["outcome"] = "failure"
            evidence["stage"] = "evidence_write"
            evidence["failure_code"] = "evidence_bucket_write_failed"
    evidence["logging_write"] = {"completed": True}
    try:
        runtime.logger.write(evidence)
    except Exception:
        evidence["logging_write"] = {
            "completed": False,
            "failure_code": "evidence_logging_write_failed",
        }
        if evidence["outcome"] == "success":
            evidence["outcome"] = "failure"
            evidence["stage"] = "evidence_write"
            evidence["failure_code"] = "evidence_logging_write_failed"
        if storage_written:
            failure_object = object_name.removesuffix(".json") + "-failure.json"
            evidence["evidence_failure_object"] = (
                f"gs://{config.bucket_name}/{failure_object}"
            )
            try:
                runtime.storage.write_evidence(failure_object, evidence)
            except Exception:
                pass
        return 1, evidence
    if not storage_written:
        return 1, evidence
    return (0 if evidence["outcome"] == "success" else 1), evidence


def _validate_proxy_binary(path_text: str) -> None:
    path = Path(path_text)
    try:
        info = path.lstat()
    except OSError as exc:
        raise DrillError("configuration", "proxy_binary_unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or not (info.st_mode & stat.S_IXUSR)
        or info.st_mode & 0o022
    ):
        raise DrillError("configuration", "proxy_binary_insecure")


def build_runtime(config: Config) -> Runtime:
    _validate_proxy_binary(config.proxy_binary)
    try:
        import google.auth
        import psycopg
        from google.auth.transport.requests import AuthorizedSession
        from google.cloud import logging as cloud_logging
        from google.cloud import storage
    except ImportError as exc:  # pragma: no cover - container build invariant.
        raise DrillError("configuration", "runtime_dependency_missing") from exc

    credentials, detected_project = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    if detected_project and detected_project != config.recovery_project_id:
        raise DrillError("configuration", "credentials_project_mismatch")
    session = AuthorizedSession(credentials)
    return Runtime(
        storage=RecoveryStorage(
            storage.Client(
                project=config.recovery_project_id, credentials=credentials
            ),
            config,
        ),
        logger=RecoveryLogger(
            cloud_logging.Client(
                project=config.recovery_project_id, credentials=credentials
            )
        ),
        sql=SqlAdminRest(session, config.recovery_project_id),
        db_connect=psycopg.connect,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        args = list(sys.argv[1:] if argv is None else argv)
        if args not in ([], ["--janitor-only"]):
            raise DrillError("configuration", "invalid_arguments")
        config = Config.from_env()
        runtime = build_runtime(config)
        if args == ["--janitor-only"]:
            now = runtime.now().astimezone(timezone.utc)
            removed = _reconcile_drill_instances(runtime, config, now)
            event = {
                "event": "db_restore_drill_janitor",
                "outcome": "success",
                "removed_count": len(removed),
                "finished_at": now.isoformat(),
            }
            runtime.logger.write(event)
            print(json.dumps(event, sort_keys=True))
            return 0
        exit_code, evidence = run_drill(config, runtime)
        print(
            json.dumps(
                {
                    "event": evidence["event"],
                    "drill_id": evidence["drill_id"],
                    "outcome": evidence["outcome"],
                    "stage": evidence["stage"],
                    "evidence_object": evidence["evidence_object"],
                },
                sort_keys=True,
            )
        )
        return exit_code
    except DrillError as exc:
        print(
            json.dumps(
                {
                    "event": "db_restore_drill",
                    "outcome": "failure",
                    "stage": exc.stage,
                    "failure_code": exc.code,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            '{"event":"db_restore_drill","failure_code":"unexpected_error",'
            '"outcome":"failure","stage":"bootstrap"}',
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
