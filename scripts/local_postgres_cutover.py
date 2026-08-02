#!/usr/bin/env python3
"""Fail-closed direct-pg_dump gate for the Cloud SQL -> local PG cutover.

The tool never accepts a database password on the command line.  Database
inspection and import run through ``docker exec`` with an argv-only psql
command.  If a password file is supplied, its value is forwarded through the
Docker client's environment (``--env PGPASSWORD``), never argv or evidence.

Two commands form the cutover protocol:

``dump-source``
    Validate a durable source fence, snapshot the source, and create a pinned
    PostgreSQL 15 custom-format archive directly through the authenticated
    proxy.  Managed Cloud SQL SQL exports are intentionally unsupported because
    they omit triggers and stored procedures.

``verify-import``
    Validate the source fence, direct-dump manifest and source snapshot; prove
    the target public schema empty; restore the held archive descriptor in one
    transaction; snapshot the target; and require an exact evidence match.

For a Cloud SQL source behind the standard host Auth Proxy, execute psql from
the pinned PostgreSQL container and point it at the Docker bridge gateway:

``dump-source --container blueprint-local-postgres --host 172.22.0.1 --port 5433``

The durable-fence document binds raw Cloud Run IAM, Cloud SQL instance, password
rotation operation, and three zero-session observations by absolute path and
SHA-256.  The observations must span at least 30 seconds. ``dump-source`` adds
fresh pre/post-dump session proofs and atomically emits the source snapshot and
direct-dump manifest. Evidence and archives belong in a root-only directory.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping, Sequence

SCHEMA_VERSION = 1
SNAPSHOT_KIND = "postgres-cutover-snapshot"
FENCE_KIND = "postgres-durable-source-fence"
MANIFEST_KIND = "postgres-direct-dump-manifest"
RESULT_KIND = "postgres-cutover-result"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,255}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$")
_CRITICAL_TABLES = frozenset({"alloweduser", "project", "task", "artifact"})
_MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
_CUTOVER_APP_PREFIX = "blueprint-cutover-"
_OBSERVER_APP_NAME = _CUTOVER_APP_PREFIX + "observer"


class CutoverError(RuntimeError):
    """Expected fail-closed outcome carrying a non-sensitive stage/code."""

    def __init__(self, stage: str, code: str):
        self.stage = stage
        self.code = code
        super().__init__(f"{stage}:{code}")


def _utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise CutoverError("evidence", f"missing_{field}")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise CutoverError("evidence", f"invalid_{field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CutoverError("evidence", f"naive_{field}")
    return parsed.astimezone(dt.timezone.utc)


def _safe_text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or any(
        ord(char) < 32 or ord(char) == 127 for char in value
    ):
        raise CutoverError("configuration", f"invalid_{field}")
    return value


def _safe_cli_name(value: str, field: str) -> str:
    value = _safe_text(value, field)
    if not _SAFE_NAME_RE.fullmatch(value):
        raise CutoverError("configuration", f"unsafe_{field}")
    return value


def _reject_symlink_components(path: Path) -> None:
    current = path
    components: list[Path] = []
    while True:
        components.append(current)
        if current == current.parent:
            break
        current = current.parent
    for component in reversed(components):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise CutoverError("filesystem", "symlink_path_component")


def _regular_input(path_value: str, label: str, *, max_bytes: int | None = None) -> Path:
    path = Path(path_value)
    if not path.is_absolute():
        raise CutoverError("filesystem", f"{label}_not_absolute")
    _reject_symlink_components(path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise CutoverError("filesystem", f"{label}_missing") from exc
    if not stat.S_ISREG(info.st_mode):
        raise CutoverError("filesystem", f"{label}_not_regular")
    if max_bytes is not None and info.st_size > max_bytes:
        raise CutoverError("filesystem", f"{label}_too_large")
    return path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _open_held_regular(path_value: str, label: str) -> tuple[int, Path, os.stat_result]:
    path = Path(path_value)
    if not path.is_absolute():
        raise CutoverError("filesystem", f"{label}_not_absolute")
    _reject_symlink_components(path.parent)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CutoverError("filesystem", f"{label}_open_failed") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CutoverError("filesystem", f"{label}_not_single_regular")
        resolved = path.stat()
        if (resolved.st_dev, resolved.st_ino) != (info.st_dev, info.st_ino):
            raise CutoverError("filesystem", f"{label}_identity_changed")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, path, info


def _fd_sha256(descriptor: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest(), size


def _load_json(path_value: str, label: str) -> tuple[dict[str, Any], Path, str]:
    path = _regular_input(path_value, label, max_bytes=_MAX_EVIDENCE_BYTES)
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CutoverError("evidence", f"invalid_{label}_json") from exc
    if not isinstance(value, dict):
        raise CutoverError("evidence", f"invalid_{label}_shape")
    return value, path, hashlib.sha256(raw).hexdigest()


def _atomic_json(path_value: str, payload: Mapping[str, Any]) -> Path:
    path = Path(path_value)
    if not path.is_absolute():
        raise CutoverError("filesystem", "output_not_absolute")
    parent = path.parent
    _reject_symlink_components(parent)
    try:
        parent_info = parent.lstat()
    except OSError as exc:
        raise CutoverError("filesystem", "output_parent_missing") from exc
    if not stat.S_ISDIR(parent_info.st_mode):
        raise CutoverError("filesystem", "output_parent_not_directory")
    if path.exists() or path.is_symlink():
        _reject_symlink_components(path)
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            raise CutoverError("filesystem", "unsafe_existing_output")

    encoded = (json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        # Directory fsync makes the rename durable on POSIX. Windows does not
        # provide equivalent directory-handle semantics and can block here.
        if os.name != "nt":
            directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return path


def capture_cloud_run_fence(
    args: argparse.Namespace,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    project_id = _safe_cli_name(args.project_id, "project_id")
    region = _safe_cli_name(args.region, "region")
    service_name = _safe_cli_name(args.service, "service")
    gcloud_executable = _safe_text(args.gcloud_executable, "gcloud_executable")
    gcloud_argv = [gcloud_executable]
    gcloud_environment = os.environ.copy()
    if Path(gcloud_executable).suffix.lower() == ".py":
        gcloud_script = Path(gcloud_executable)
        if not gcloud_script.is_absolute() or not gcloud_script.is_file():
            raise CutoverError("configuration", "invalid_gcloud_python_entrypoint")
        gcloud_argv = [sys.executable, str(gcloud_script)]
        gcloud_environment.setdefault(
            "CLOUDSDK_ROOT_DIR", str(gcloud_script.parent.parent)
        )

    def capture(command: list[str], label: str) -> dict[str, Any]:
        result = run(
            [*gcloud_argv, *command],
            check=False,
            capture_output=True,
            env=gcloud_environment,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise CutoverError("cloud_run_fence", f"{label}_failed")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CutoverError("cloud_run_fence", f"{label}_invalid_json") from exc
        if not isinstance(value, dict):
            raise CutoverError("cloud_run_fence", f"{label}_invalid_shape")
        return value

    payload = {
        "schema_version": SCHEMA_VERSION,
        "kind": "cloud-run-write-fence",
        "captured_at": _utcnow(),
        "request": {
            "project_id": project_id,
            "region": region,
            "service": service_name,
        },
        "project": capture(
            [
                "projects",
                "describe",
                project_id,
                "--format=json(projectId,projectNumber)",
            ],
            "project_describe",
        ),
        "service": capture(
            [
                "run",
                "services",
                "describe",
                service_name,
                f"--project={project_id}",
                f"--region={region}",
                "--format=json(metadata.name,metadata.namespace,metadata.labels,status.url)",
            ],
            "service_describe",
        ),
        "policy": capture(
            [
                "run",
                "services",
                "get-iam-policy",
                service_name,
                f"--project={project_id}",
                f"--region={region}",
                "--format=json",
            ],
            "get_iam_policy",
        ),
    }
    _atomic_json(args.output, payload)
    return payload


def _quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class DockerPsql:
    """psql client reached only through a named Docker container."""

    def __init__(
        self,
        *,
        container: str,
        database: str,
        user: str,
        password_file: str | None = None,
        host: str | None = None,
        port: int | None = None,
        docker_binary: str = "docker",
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.container = _safe_cli_name(container, "container")
        self.database = _safe_cli_name(database, "database")
        self.user = _safe_cli_name(user, "user")
        self.password_file = password_file
        if (host is None) != (port is None):
            raise CutoverError("configuration", "host_and_port_required_together")
        if host is not None and not _HOST_RE.fullmatch(host):
            raise CutoverError("configuration", "unsafe_host")
        if port is not None and not 1 <= port <= 65535:
            raise CutoverError("configuration", "invalid_port")
        self.host = host
        self.port = port
        self.docker_binary = _safe_text(docker_binary, "docker_binary")
        self._run = run

    def environment(self, *, app_name: str | None = None) -> dict[str, str]:
        environment = os.environ.copy()
        if app_name is not None:
            if not app_name.startswith(_CUTOVER_APP_PREFIX) or not _SAFE_NAME_RE.fullmatch(
                app_name
            ):
                raise CutoverError("configuration", "invalid_application_name")
            environment["PGAPPNAME"] = app_name
        if self.password_file is None:
            return environment
        path = _regular_input(self.password_file, "password_file", max_bytes=64 * 1024)
        if os.name != "nt" and stat.S_IMODE(path.lstat().st_mode) != 0o600:
            raise CutoverError("filesystem", "password_file_mode")
        raw = path.read_bytes()
        if raw.endswith(b"\n"):
            raw = raw[:-1]
        if not raw or b"\x00" in raw:
            raise CutoverError("configuration", "invalid_password_file")
        try:
            environment["PGPASSWORD"] = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CutoverError("configuration", "invalid_password_encoding") from exc
        return environment

    def argv(
        self,
        *,
        sql: str | None = None,
        import_mode: bool = False,
        app_name: str | None = None,
    ) -> list[str]:
        argv = [self.docker_binary, "exec", "--interactive"]
        if self.password_file is not None:
            # Docker copies the value from its own environment. The value is
            # absent from argv, process listings, logs, and evidence.
            argv.extend(["--env", "PGPASSWORD"])
        if app_name is not None:
            # As with PGPASSWORD, Docker copies the value from the CLI process
            # environment; the value itself is not placed in argv.
            self.environment(app_name=app_name)
            argv.extend(["--env", "PGAPPNAME"])
        argv.extend(
            [
                self.container,
                "psql",
                "--no-psqlrc",
                "--quiet",
                "--set",
                "ON_ERROR_STOP=1",
                "--username",
                self.user,
                "--dbname",
                self.database,
            ]
        )
        if import_mode:
            argv.append("--single-transaction")
        if self.host is not None:
            argv.extend(["--host", self.host, "--port", str(self.port)])
        if sql is not None:
            argv.extend(["--tuples-only", "--no-align", "--command", sql])
        return argv

    def query_text(self, sql: str, *, app_name: str | None = None) -> str:
        result = self._run(
            self.argv(sql=sql, app_name=app_name),
            capture_output=True,
            text=True,
            check=False,
            env=self.environment(app_name=app_name),
            timeout=300,
        )
        if result.returncode != 0:
            raise CutoverError("database_snapshot", "psql_query_failed")
        return result.stdout.strip()

    def pg_tool_argv(self, tool: str, *options: str) -> list[str]:
        if tool not in {"pg_dump", "pg_restore"}:
            raise CutoverError("configuration", "unsupported_postgres_tool")
        argv = [self.docker_binary, "exec", "--interactive"]
        if self.password_file is not None:
            argv.extend(["--env", "PGPASSWORD"])
        argv.extend(["--env", "PGAPPNAME", self.container, tool])
        if self.host is not None:
            argv.extend(["--host", self.host, "--port", str(self.port)])
        argv.extend(["--username", self.user, "--dbname", self.database, *options])
        return argv

    def query_json(self, sql: str, *, app_name: str | None = None) -> Any:
        text = self.query_text(sql, app_name=app_name)
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise CutoverError("database_snapshot", "invalid_psql_json") from exc


_PUBLIC_SCHEMA_SQL = """
SELECT jsonb_build_object(
  'exists', EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'public'),
  'relations', COALESCE((
    SELECT jsonb_agg(jsonb_build_object('name', c.relname, 'kind', c.relkind)
                     ORDER BY c.relname, c.relkind)
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','S','f')
  ), '[]'::jsonb),
  'routines', COALESCE((
    SELECT jsonb_agg(p.proname ORDER BY p.proname)
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
  ), '[]'::jsonb),
  'user_types', COALESCE((
    SELECT jsonb_agg(t.typname ORDER BY t.typname)
    FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname = 'public' AND t.typrelid = 0 AND t.typtype IN ('d','e','m','r')
  ), '[]'::jsonb)
)::text;
"""

_TABLES_SQL = """
SELECT COALESCE(jsonb_agg(jsonb_build_object(
  'schema', n.nspname, 'name', c.relname, 'kind', c.relkind,
  'is_partition', c.relispartition, 'owner', pg_get_userbyid(c.relowner),
  'acl', COALESCE(c.relacl::text[], ARRAY[]::text[])
) ORDER BY c.relname), '[]'::jsonb)::text
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r','p');
"""

_COLUMNS_SQL = """
SELECT COALESCE(jsonb_agg(jsonb_build_object(
  'schema', table_schema, 'table', table_name, 'name', column_name,
  'ordinal', ordinal_position, 'data_type', data_type,
  'udt_schema', udt_schema, 'udt_name', udt_name,
  'nullable', is_nullable, 'default', column_default,
  'identity', identity_generation, 'generated', is_generated
) ORDER BY table_name, ordinal_position), '[]'::jsonb)::text
FROM information_schema.columns WHERE table_schema = 'public';
"""

_CONSTRAINTS_SQL = """
SELECT COALESCE(jsonb_agg(jsonb_build_object(
  'schema', n.nspname, 'table', c.relname, 'name', con.conname,
  'type', con.contype, 'deferrable', con.condeferrable,
  'initially_deferred', con.condeferred, 'validated', con.convalidated,
  'definition', pg_get_constraintdef(con.oid, true)
) ORDER BY c.relname, con.conname), '[]'::jsonb)::text
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public';
"""

_SEQUENCE_OWNERS_SQL = """
SELECT COALESCE(jsonb_agg(jsonb_build_object(
  'sequence_schema', sn.nspname, 'sequence', seq.relname,
  'table_schema', tn.nspname, 'table', tbl.relname,
  'column', att.attname, 'increment', s.seqincrement,
  'owner', pg_get_userbyid(seq.relowner),
  'acl', COALESCE(seq.relacl::text[], ARRAY[]::text[])
) ORDER BY seq.relname), '[]'::jsonb)::text
FROM pg_class seq
JOIN pg_namespace sn ON sn.oid = seq.relnamespace
JOIN pg_sequence s ON s.seqrelid = seq.oid
JOIN pg_depend dep ON dep.objid = seq.oid AND dep.classid = 'pg_class'::regclass
  AND dep.refclassid = 'pg_class'::regclass AND dep.deptype IN ('a','i')
JOIN pg_class tbl ON tbl.oid = dep.refobjid
JOIN pg_namespace tn ON tn.oid = tbl.relnamespace
JOIN pg_attribute att ON att.attrelid = tbl.oid AND att.attnum = dep.refobjsubid
WHERE sn.nspname = 'public' AND tn.nspname = 'public' AND seq.relkind = 'S';
"""

_INDEXES_SQL = """
SELECT COALESCE(jsonb_agg(jsonb_build_object(
  'schema', x.schemaname, 'table', x.tablename, 'name', x.indexname,
  'definition', x.indexdef, 'owner', pg_get_userbyid(i.relowner),
  'acl', COALESCE(i.relacl::text[], ARRAY[]::text[]),
  'valid', state.indisvalid, 'ready', state.indisready, 'live', state.indislive
) ORDER BY x.tablename, x.indexname), '[]'::jsonb)::text
FROM pg_indexes x
JOIN pg_namespace n ON n.nspname=x.schemaname
JOIN pg_class i ON i.relnamespace=n.oid AND i.relname=x.indexname
JOIN pg_index state ON state.indexrelid=i.oid
WHERE x.schemaname = 'public';
"""

_TRIGGERS_SQL = """
SELECT COALESCE(jsonb_agg(jsonb_build_object(
  'schema', n.nspname, 'table', c.relname, 'name', t.tgname,
  'enabled', t.tgenabled, 'definition', pg_get_triggerdef(t.oid, true)
) ORDER BY c.relname, t.tgname), '[]'::jsonb)::text
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND NOT t.tgisinternal;
"""

_VIEWS_SQL = """
SELECT COALESCE(jsonb_agg(jsonb_build_object(
  'schema', n.nspname, 'name', c.relname, 'kind', c.relkind,
  'owner', pg_get_userbyid(c.relowner), 'acl', COALESCE(c.relacl::text[], ARRAY[]::text[]),
  'options', COALESCE(c.reloptions, ARRAY[]::text[]),
  'definition', pg_get_viewdef(c.oid, true)
) ORDER BY c.relkind, c.relname), '[]'::jsonb)::text
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('v','m');
"""

_ROUTINES_SQL = """
SELECT COALESCE(jsonb_agg(jsonb_build_object(
  'schema', n.nspname, 'name', p.proname,
  'identity_arguments', pg_get_function_identity_arguments(p.oid),
  'kind', p.prokind, 'owner', pg_get_userbyid(p.proowner),
  'language', l.lanname, 'volatility', p.provolatile, 'parallel', p.proparallel,
  'security_definer', p.prosecdef, 'leakproof', p.proleakproof,
  'strict', p.proisstrict, 'acl', COALESCE(p.proacl::text[], ARRAY[]::text[]),
  'configuration', COALESCE(p.proconfig, ARRAY[]::text[]),
  'definition', CASE WHEN p.prokind IN ('f','p') THEN pg_get_functiondef(p.oid) ELSE NULL END
) ORDER BY p.proname, pg_get_function_identity_arguments(p.oid)), '[]'::jsonb)::text
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
JOIN pg_language l ON l.oid = p.prolang
WHERE n.nspname = 'public';
"""

_TYPES_SQL = """
SELECT COALESCE(jsonb_agg(jsonb_build_object(
  'schema', n.nspname, 'name', t.typname, 'kind', t.typtype,
  'owner', pg_get_userbyid(t.typowner), 'acl', COALESCE(t.typacl::text[], ARRAY[]::text[]),
  'not_null', t.typnotnull, 'default', t.typdefault,
  'base_type', CASE WHEN t.typbasetype = 0 THEN NULL ELSE format_type(t.typbasetype, t.typtypmod) END,
  'enum_labels', COALESCE((SELECT jsonb_agg(e.enumlabel ORDER BY e.enumsortorder)
                           FROM pg_enum e WHERE e.enumtypid=t.oid), '[]'::jsonb),
  'range_subtype', CASE WHEN r.rngsubtype IS NULL THEN NULL ELSE format_type(r.rngsubtype, NULL) END
) ORDER BY t.typname), '[]'::jsonb)::text
FROM pg_type t
JOIN pg_namespace n ON n.oid=t.typnamespace
LEFT JOIN pg_range r ON r.rngtypid=t.oid
WHERE n.nspname='public' AND t.typrelid=0 AND t.typtype IN ('d','e','m','r');
"""

_EXTENSIONS_SQL = """
SELECT COALESCE(jsonb_agg(jsonb_build_object(
  'name', e.extname, 'version', e.extversion, 'schema', n.nspname,
  'owner', pg_get_userbyid(e.extowner), 'relocatable', e.extrelocatable
) ORDER BY e.extname), '[]'::jsonb)::text
FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
WHERE e.extname <> 'plpgsql';
"""

_CRITICAL_SQL = """
SELECT jsonb_build_object(
  'alloweduser_count', (SELECT count(*) FROM public.alloweduser),
  'project_by_status', COALESCE((SELECT jsonb_object_agg(status, amount)
    FROM (SELECT status::text, count(*) AS amount FROM public.project GROUP BY status) q), '{}'::jsonb),
  'task_by_status', COALESCE((SELECT jsonb_object_agg(status, amount)
    FROM (SELECT status::text, count(*) AS amount FROM public.task GROUP BY status) q), '{}'::jsonb),
  'artifact_by_type', COALESCE((SELECT jsonb_object_agg(artifact_type, amount)
    FROM (SELECT artifact_type::text, count(*) AS amount FROM public.artifact GROUP BY artifact_type) q), '{}'::jsonb),
  'orphan_tasks', (SELECT count(*) FROM public.task t LEFT JOIN public.project p ON p.id=t.project_id WHERE p.id IS NULL),
  'orphan_parent_tasks', (SELECT count(*) FROM public.task t LEFT JOIN public.task p ON p.id=t.parent_task_id WHERE t.parent_task_id IS NOT NULL AND p.id IS NULL),
  'orphan_artifacts', (SELECT count(*) FROM public.artifact a LEFT JOIN public.task t ON t.id=a.task_id WHERE t.id IS NULL)
)::text;
"""


def target_public_schema_empty(client: DockerPsql) -> dict[str, Any]:
    value = client.query_json(_PUBLIC_SCHEMA_SQL)
    if not isinstance(value, dict) or value.get("exists") is not True:
        raise CutoverError("target_preflight", "public_schema_missing")
    for key in ("relations", "routines", "user_types"):
        if not isinstance(value.get(key), list):
            raise CutoverError("target_preflight", "invalid_schema_inventory")
        if value[key]:
            raise CutoverError("target_preflight", "public_schema_not_empty")
    return value


def _sequence_snapshot(client: DockerPsql) -> list[dict[str, Any]]:
    owners = client.query_json(_SEQUENCE_OWNERS_SQL)
    if not isinstance(owners, list):
        raise CutoverError("database_snapshot", "invalid_sequence_inventory")
    result: list[dict[str, Any]] = []
    for owner in owners:
        if not isinstance(owner, dict):
            raise CutoverError("database_snapshot", "invalid_sequence_owner")
        increment = owner.get("increment")
        if not isinstance(increment, int) or increment <= 0:
            raise CutoverError("database_snapshot", "unsupported_sequence_increment")
        seq_name = f"{_quote_ident(str(owner['sequence_schema']))}.{_quote_ident(str(owner['sequence']))}"
        table_name = f"{_quote_ident(str(owner['table_schema']))}.{_quote_ident(str(owner['table']))}"
        column = _quote_ident(str(owner["column"]))
        state = client.query_json(
            f"SELECT jsonb_build_object('last_value', last_value, 'is_called', is_called)::text FROM {seq_name};"
        )
        maximum = client.query_json(f"SELECT to_jsonb(max({column}))::text FROM {table_name};")
        if not isinstance(state, dict) or not isinstance(state.get("last_value"), int):
            raise CutoverError("database_snapshot", "invalid_sequence_state")
        if maximum is not None and not isinstance(maximum, int):
            raise CutoverError("database_snapshot", "non_integer_owned_sequence")
        next_value = (
            state["last_value"] + increment
            if state.get("is_called") is True
            else state["last_value"]
        )
        if not isinstance(state.get("is_called"), bool):
            raise CutoverError("database_snapshot", "invalid_sequence_called_state")
        if maximum is not None and next_value <= maximum:
            raise CutoverError("database_snapshot", "sequence_behind_owned_column")
        result.append(
            {**owner, **state, "next_value": next_value, "owned_column_max": maximum}
        )
    return sorted(result, key=lambda item: (item["sequence_schema"], item["sequence"]))


def snapshot_database(client: DockerPsql, *, label: str, instance: str) -> dict[str, Any]:
    tables = client.query_json(_TABLES_SQL)
    if not isinstance(tables, list) or not tables:
        raise CutoverError("database_snapshot", "no_public_tables")
    names = {item.get("name") for item in tables if isinstance(item, dict)}
    if not _CRITICAL_TABLES.issubset(names):
        raise CutoverError("database_snapshot", "critical_tables_missing")

    revisions = client.query_json(
        "SELECT COALESCE(jsonb_agg(version_num ORDER BY version_num), '[]'::jsonb)::text FROM public.alembic_version;"
    )
    if not isinstance(revisions, list) or len(revisions) != 1 or not isinstance(revisions[0], str):
        raise CutoverError("database_snapshot", "invalid_alembic_revision")
    columns = client.query_json(_COLUMNS_SQL)
    constraints = client.query_json(_CONSTRAINTS_SQL)
    indexes = client.query_json(_INDEXES_SQL)
    triggers = client.query_json(_TRIGGERS_SQL)
    views = client.query_json(_VIEWS_SQL)
    routines = client.query_json(_ROUTINES_SQL)
    types = client.query_json(_TYPES_SQL)
    extensions = client.query_json(_EXTENSIONS_SQL)
    critical = client.query_json(_CRITICAL_SQL)
    if (
        not isinstance(columns, list)
        or not isinstance(constraints, list)
        or not isinstance(indexes, list)
        or not isinstance(triggers, list)
        or not isinstance(views, list)
        or not isinstance(routines, list)
        or not isinstance(types, list)
        or not isinstance(extensions, list)
        or not isinstance(critical, dict)
    ):
        raise CutoverError("database_snapshot", "invalid_inventory_shape")
    required_constraint_fields = {
        "schema",
        "table",
        "name",
        "type",
        "deferrable",
        "initially_deferred",
        "validated",
        "definition",
    }
    for constraint in constraints:
        if (
            not isinstance(constraint, dict)
            or not required_constraint_fields.issubset(constraint)
            or not isinstance(constraint.get("validated"), bool)
            or not isinstance(constraint.get("deferrable"), bool)
            or not isinstance(constraint.get("initially_deferred"), bool)
            or not isinstance(constraint.get("definition"), str)
        ):
            raise CutoverError("database_snapshot", "invalid_constraint_inventory")
    unvalidated_constraints = [
        constraint for constraint in constraints if constraint["validated"] is False
    ]
    if unvalidated_constraints:
        raise CutoverError("database_snapshot", "unvalidated_constraints_present")
    required_index_fields = {
        "schema",
        "table",
        "name",
        "definition",
        "owner",
        "acl",
        "valid",
        "ready",
        "live",
    }
    for index in indexes:
        if (
            not isinstance(index, dict)
            or not required_index_fields.issubset(index)
            or not isinstance(index.get("definition"), str)
            or not all(
                isinstance(index.get(field), bool)
                for field in ("valid", "ready", "live")
            )
        ):
            raise CutoverError("database_snapshot", "invalid_index_inventory")
    if any(
        not index["valid"] or not index["ready"] or not index["live"]
        for index in indexes
    ):
        raise CutoverError("database_snapshot", "unhealthy_indexes_present")
    for key in ("orphan_tasks", "orphan_parent_tasks", "orphan_artifacts"):
        if critical.get(key) != 0:
            raise CutoverError("database_snapshot", f"critical_{key}")

    row_counts: dict[str, int] = {}
    for item in tables:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise CutoverError("database_snapshot", "invalid_table_inventory")
        name = item["name"]
        count = client.query_json(
            f"SELECT to_jsonb(count(*))::text FROM public.{_quote_ident(name)};"
        )
        if not isinstance(count, int) or count < 0:
            raise CutoverError("database_snapshot", "invalid_row_count")
        row_counts[name] = count

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": SNAPSHOT_KIND,
        "captured_at": _utcnow(),
        "label": _safe_text(label, "label"),
        "instance": _safe_text(instance, "instance"),
        "database": client.database,
        "alembic_revisions": revisions,
        "public_tables": tables,
        "public_columns": columns,
        "public_constraints": constraints,
        "public_indexes": indexes,
        "public_triggers": triggers,
        "public_views": views,
        "public_routines": routines,
        "public_types": types,
        "extensions": extensions,
        "row_counts": dict(sorted(row_counts.items())),
        "unvalidated_constraints": unvalidated_constraints,
        "sequences": _sequence_snapshot(client),
        "critical_aggregates": critical,
    }


_COMPARISON_FIELDS = (
    "alembic_revisions",
    "public_tables",
    "public_columns",
    "public_constraints",
    "public_indexes",
    "public_triggers",
    "public_views",
    "public_routines",
    "public_types",
    "extensions",
    "row_counts",
    "unvalidated_constraints",
    "sequences",
    "critical_aggregates",
)


def validate_snapshot(value: Mapping[str, Any], *, label: str) -> None:
    if value.get("schema_version") != SCHEMA_VERSION or value.get("kind") != SNAPSHOT_KIND:
        raise CutoverError("evidence", f"invalid_{label}_snapshot_header")
    _timestamp(value.get("captured_at"), f"{label}_snapshot_captured_at")
    for field in _COMPARISON_FIELDS:
        if field not in value:
            raise CutoverError("evidence", f"missing_{label}_{field}")
    revisions = value.get("alembic_revisions")
    if not isinstance(revisions, list) or len(revisions) != 1 or not isinstance(revisions[0], str):
        raise CutoverError("evidence", f"invalid_{label}_alembic_revision")
    if value.get("unvalidated_constraints") != []:
        raise CutoverError("evidence", f"{label}_unvalidated_constraints")
    for field in ("public_constraints", "public_indexes"):
        inventory = value.get(field)
        if not isinstance(inventory, list) or any(
            not isinstance(item, dict)
            or "definition" not in item
            or not isinstance(item["definition"], str)
            for item in inventory
        ):
            raise CutoverError("evidence", f"invalid_{label}_{field}")
    critical = value.get("critical_aggregates")
    if not isinstance(critical, dict) or any(
        critical.get(key) != 0
        for key in ("orphan_tasks", "orphan_parent_tasks", "orphan_artifacts")
    ):
        raise CutoverError("evidence", f"{label}_critical_integrity")


def _normalize_acl_item(value: Any, role: str) -> Any:
    if not isinstance(value, str) or "=" not in value or "/" not in value:
        return value
    grantee_and_privileges, grantor = value.rsplit("/", 1)
    grantee, privileges = grantee_and_privileges.split("=", 1)
    if grantee == role:
        grantee = "<cutover-owner>"
    if grantor == role:
        grantor = "<cutover-owner>"
    return f"{grantee}={privileges}/{grantor}"


def _normalize_role_bound(value: Any, role: str) -> Any:
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if key == "owner":
                normalized[key] = "<cutover-owner>" if item == role else item
            elif key == "acl" and isinstance(item, list):
                normalized[key] = [_normalize_acl_item(entry, role) for entry in item]
            else:
                normalized[key] = _normalize_role_bound(item, role)
        return normalized
    if isinstance(value, list):
        return [_normalize_role_bound(item, role) for item in value]
    return value


_TEXT_ARRAY_BODY_RE = r"[^\[\]]*"
_WRAPPED_TEXT_ARRAY_RE = re.compile(
    rf"\(ARRAY\[(?P<body>{_TEXT_ARRAY_BODY_RE})\]\)::text\[\]"
)
_CAST_TEXT_ARRAY_RE = re.compile(
    rf"ARRAY\[(?P<body>{_TEXT_ARRAY_BODY_RE})\]::text\[\]"
)
_TEXT_ARRAY_RE = re.compile(rf"ARRAY\[(?P<body>{_TEXT_ARRAY_BODY_RE})\]")
_TEXT_ARRAY_ELEMENT_RE = re.compile(
    r"(?:"
    r"\((?P<parenthesized>'(?:''|[^'])*')::character varying\)::text"
    r"|(?P<varchar>'(?:''|[^'])*')::character varying(?:::text)?"
    r"|(?P<text>'(?:''|[^'])*')::text"
    r")"
)


def _canonical_text_literal_array(match: re.Match[str]) -> str:
    """Canonicalize only an array made entirely of cast string literals."""

    body = match.group("body")
    allows_bare_varchar = match.re in {
        _WRAPPED_TEXT_ARRAY_RE,
        _CAST_TEXT_ARRAY_RE,
    }
    literals: list[str] = []
    position = 0
    while position < len(body):
        element = _TEXT_ARRAY_ELEMENT_RE.match(body, position)
        if element is None:
            return match.group(0)
        if (
            element.group("varchar") is not None
            and not allows_bare_varchar
            and not element.group(0).endswith("::text")
        ):
            return match.group(0)
        literal = next(value for value in element.groups() if value is not None)
        literals.append(f"{literal}::text")
        position = element.end()
        if position == len(body):
            break
        separator = re.match(r",\s*", body[position:])
        if separator is None:
            return match.group(0)
        position += separator.end()
    if not literals:
        return match.group(0)
    return "ARRAY[" + ", ".join(literals) + "]"


def _normalize_pg15_definition(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    # PostgreSQL 15.17 emits ``(ARRAY[varchar literals])::text[]`` while
    # 15.18 emits ``ARRAY[(varchar literal)::text]``.  The rewrite is allowed
    # only when every array element is an explicitly cast SQL string literal.
    result: list[str] = []
    position = 0
    while position < len(value):
        character = value[position]
        if character in {"'", '"'}:
            quote = character
            end = position + 1
            closed = False
            while end < len(value):
                if value[end] == quote:
                    if end + 1 < len(value) and value[end + 1] == quote:
                        end += 2
                        continue
                    end += 1
                    closed = True
                    break
                end += 1
            if not closed:
                return value
            result.append(value[position:end])
            position = end
            continue
        if value.startswith("--", position):
            end = value.find("\n", position + 2)
            if end < 0:
                result.append(value[position:])
                break
            result.append(value[position:end])
            position = end
            continue
        if value.startswith("/*", position):
            end = position + 2
            depth = 1
            while end < len(value) and depth:
                if value.startswith("/*", end):
                    depth += 1
                    end += 2
                elif value.startswith("*/", end):
                    depth -= 1
                    end += 2
                else:
                    end += 1
            if depth:
                return value
            result.append(value[position:end])
            position = end
            continue
        if character == "$":
            tag = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", value[position:])
            if tag is not None:
                delimiter = tag.group(0)
                end = value.find(delimiter, position + len(delimiter))
                if end < 0:
                    return value
                end += len(delimiter)
                result.append(value[position:end])
                position = end
                continue
        candidates: list[re.Match[str]] = []
        if character == "(":
            wrapped = _WRAPPED_TEXT_ARRAY_RE.match(value, position)
            if wrapped is not None:
                candidates.append(wrapped)
        if value.startswith("ARRAY[", position) and (
            position == 0
            or not (value[position - 1].isalnum() or value[position - 1] in {"_", "$"})
        ):
            cast_array = _CAST_TEXT_ARRAY_RE.match(value, position)
            if cast_array is not None:
                candidates.append(cast_array)
            array = _TEXT_ARRAY_RE.match(value, position)
            if array is not None:
                candidates.append(array)
        if candidates:
            match = max(candidates, key=lambda candidate: candidate.end())
            replacement = _canonical_text_literal_array(match)
            result.append(replacement)
            position = match.end()
            continue
        result.append(character)
        position += 1
    return "".join(result)


def _normalize_column_ordinals(value: Any) -> Any:
    """Densify ordinals while retaining exact surviving-column order."""

    if not isinstance(value, list):
        return value
    normalized: list[dict[str, Any]] = []
    prior_table: tuple[str, str] | None = None
    prior_ordinal = 0
    dense_ordinal = 0
    identities: set[tuple[str, str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            return value
        schema = item.get("schema")
        table = item.get("table")
        name = item.get("name")
        ordinal = item.get("ordinal")
        if (
            not all(isinstance(part, str) for part in (schema, table, name))
            or not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal < 1
        ):
            return value
        table_key = (schema, table)
        identity = (schema, table, name)
        if identity in identities or (prior_table is not None and table_key < prior_table):
            return value
        if table_key != prior_table:
            prior_table = table_key
            prior_ordinal = 0
            dense_ordinal = 0
        if ordinal <= prior_ordinal:
            return value
        identities.add(identity)
        prior_ordinal = ordinal
        dense_ordinal += 1
        normalized_item = dict(item)
        normalized_item["ordinal"] = dense_ordinal
        normalized.append(normalized_item)
    return normalized


def _normalize_comparison_field(field: str, value: Any, role: str) -> Any:
    normalized = _normalize_role_bound(value, role)
    if field == "public_columns":
        return _normalize_column_ordinals(normalized)
    if field in {"public_constraints", "public_indexes"} and isinstance(
        normalized, list
    ):
        result: list[Any] = []
        for item in normalized:
            if (
                not isinstance(item, dict)
                or "definition" not in item
                or not isinstance(item["definition"], str)
            ):
                return normalized
            normalized_item = dict(item)
            normalized_item["definition"] = _normalize_pg15_definition(item["definition"])
            result.append(normalized_item)
        return result
    return normalized


def snapshot_mismatches(
    source: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    source_owner: str = "postgres",
    target_owner: str = "blueprint",
    compare_sequences: bool = True,
) -> list[str]:
    validate_snapshot(source, label="source")
    validate_snapshot(target, label="target")
    source_owner = _safe_cli_name(source_owner, "source_owner")
    target_owner = _safe_cli_name(target_owner, "target_owner")
    comparison_fields = (
        _COMPARISON_FIELDS
        if compare_sequences
        else tuple(field for field in _COMPARISON_FIELDS if field != "sequences")
    )
    return [
        field
        for field in comparison_fields
        if _normalize_comparison_field(field, source[field], source_owner)
        != _normalize_comparison_field(field, target[field], target_owner)
    ]


def compare_snapshots(
    source: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    source_owner: str = "postgres",
    target_owner: str = "blueprint",
    compare_sequences: bool = True,
) -> None:
    mismatches = snapshot_mismatches(
        source,
        target,
        source_owner=source_owner,
        target_owner=target_owner,
        compare_sequences=compare_sequences,
    )
    if mismatches:
        raise CutoverError("comparison", "mismatch_" + "_".join(mismatches))


def _load_bound_raw(item: Mapping[str, Any], label: str) -> Any:
    expected = item.get("sha256")
    if not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected):
        raise CutoverError("source_fence", f"invalid_{label}_sha256")
    path = _regular_input(str(item.get("path", "")), label, max_bytes=_MAX_EVIDENCE_BYTES)
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CutoverError("source_fence", f"invalid_{label}_json") from exc
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise CutoverError("source_fence", f"{label}_digest_mismatch")
    return value


def validate_durable_fence(
    fence: Mapping[str, Any], *, expected_source: Mapping[str, str] | None = None
) -> dict[str, Any]:
    if fence.get("schema_version") != SCHEMA_VERSION or fence.get("kind") != FENCE_KIND:
        raise CutoverError("source_fence", "invalid_header")
    source = fence.get("source")
    if not isinstance(source, dict):
        raise CutoverError("source_fence", "missing_source")
    for field in (
        "project_id",
        "instance",
        "database",
        "cloud_run_service",
        "cloud_run_region",
    ):
        if not isinstance(source.get(field), str) or not source[field]:
            raise CutoverError("source_fence", f"missing_source_{field}")
    if expected_source is not None and any(
        source.get(key) != value for key, value in expected_source.items()
    ):
        raise CutoverError("source_fence", "source_identity_mismatch")
    if fence.get("password_rotated") is not True:
        raise CutoverError("source_fence", "password_not_rotated")
    if fence.get("old_sessions_terminated") is not True:
        raise CutoverError("source_fence", "old_sessions_not_terminated")

    raw_items = fence.get("raw_evidence")
    if not isinstance(raw_items, list):
        raise CutoverError("source_fence", "missing_raw_evidence")
    by_kind: dict[str, Any] = {}
    for item in raw_items:
        if not isinstance(item, dict) or not isinstance(item.get("kind"), str):
            raise CutoverError("source_fence", "invalid_raw_evidence")
        kind = item["kind"]
        if kind in by_kind:
            raise CutoverError("source_fence", "duplicate_raw_evidence")
        by_kind[kind] = _load_bound_raw(item, f"raw_{kind}")
    required = {"cloud_run_fence", "cloud_sql_instance", "password_rotation_operation"}
    if not required.issubset(by_kind):
        raise CutoverError("source_fence", "incomplete_raw_evidence")

    cloud_run = by_kind["cloud_run_fence"]
    if (
        not isinstance(cloud_run, dict)
        or cloud_run.get("schema_version") != SCHEMA_VERSION
        or cloud_run.get("kind") != "cloud-run-write-fence"
    ):
        raise CutoverError("source_fence", "invalid_cloud_run_fence")
    request = cloud_run.get("request")
    project = cloud_run.get("project")
    service = cloud_run.get("service")
    policy = cloud_run.get("policy")
    if (
        not isinstance(request, dict)
        or request.get("project_id") != source["project_id"]
        or request.get("service") != source.get("cloud_run_service")
        or request.get("region") != source.get("cloud_run_region")
        or not isinstance(project, dict)
        or project.get("projectId") != source["project_id"]
        or not isinstance(project.get("projectNumber"), str)
        or not isinstance(service, dict)
        or not isinstance(policy, dict)
    ):
        raise CutoverError("source_fence", "cloud_run_identity_mismatch")
    metadata = service.get("metadata")
    labels = metadata.get("labels") if isinstance(metadata, dict) else None
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != source.get("cloud_run_service")
        or str(metadata.get("namespace")) != project["projectNumber"]
        or not isinstance(labels, dict)
        or labels.get("cloud.googleapis.com/location") != source.get("cloud_run_region")
    ):
        raise CutoverError("source_fence", "cloud_run_identity_mismatch")
    bindings = policy.get("bindings", [])
    if not isinstance(bindings, list):
        raise CutoverError("source_fence", "invalid_cloud_run_iam_policy")
    for binding in bindings:
        if not isinstance(binding, dict) or not isinstance(binding.get("role"), str):
            raise CutoverError("source_fence", "invalid_cloud_run_iam_policy")
        members = binding.get("members", [])
        if not isinstance(members, list) or not all(
            isinstance(member, str) for member in members
        ):
            raise CutoverError("source_fence", "invalid_cloud_run_iam_policy")
        if binding["role"] == "roles/run.invoker" and any(
            member in {"allUsers", "allAuthenticatedUsers"} for member in members
        ):
            raise CutoverError("source_fence", "cloud_run_public_invoker_enabled")
    if "version" in policy and not isinstance(policy["version"], int):
        raise CutoverError("source_fence", "invalid_cloud_run_iam_policy")
    if any(
        "condition" in binding
        and not isinstance(binding["condition"], dict)
        for binding in bindings
    ):
        raise CutoverError("source_fence", "invalid_cloud_run_iam_policy")
    instance = by_kind["cloud_sql_instance"]
    connection_name = (
        instance.get("connectionName") if isinstance(instance, dict) else None
    )
    if (
        not isinstance(instance, dict)
        or instance.get("name") != source["instance"]
        or instance.get("databaseVersion") != "POSTGRES_15"
        or connection_name
        != f"{source['project_id']}:{instance.get('region')}:{source['instance']}"
    ):
        raise CutoverError("source_fence", "cloud_sql_identity_mismatch")

    rotation = by_kind["password_rotation_operation"]
    if (
        not isinstance(rotation, dict)
        or rotation.get("status") != "DONE"
        or rotation.get("operationType") != "UPDATE_USER"
        or rotation.get("targetId") != source["instance"]
        or rotation.get("targetProject") != source["project_id"]
        or rotation.get("error") not in (None, "", [])
    ):
        raise CutoverError("source_fence", "invalid_password_rotation_operation")

    observations = fence.get("session_observations")
    if not isinstance(observations, list) or len(observations) < 3:
        raise CutoverError("source_fence", "insufficient_session_observations")
    previous: dt.datetime | None = None
    first: dt.datetime | None = None
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict):
            raise CutoverError("source_fence", "invalid_session_observation")
        captured = _timestamp(
            observation.get("captured_at"), f"session_observation_{index}_captured_at"
        )
        if previous is not None and captured - previous < dt.timedelta(seconds=10):
            raise CutoverError("source_fence", "session_observations_too_close")
        if first is None:
            first = captured
        previous = captured
        raw = _load_bound_raw(observation, f"session_observation_{index}")
        if raw != [] or observation.get("old_session_count") != 0:
            raise CutoverError("source_fence", "old_sessions_present")
    if first is None or previous is None or previous - first < dt.timedelta(seconds=30):
        raise CutoverError("source_fence", "session_observation_window_too_short")
    return dict(source)


def validate_direct_manifest(
    *,
    source_snapshot: Mapping[str, Any],
    source_snapshot_sha256: str,
    fence: Mapping[str, Any],
    fence_sha256: str,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    validate_snapshot(source_snapshot, label="source")
    source = validate_durable_fence(fence)
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("kind") != MANIFEST_KIND:
        raise CutoverError("dump_manifest", "invalid_header")
    if manifest.get("source") != source:
        raise CutoverError("dump_manifest", "source_identity_mismatch")
    if source_snapshot.get("instance") != source["instance"] or source_snapshot.get(
        "database"
    ) != source["database"]:
        raise CutoverError("dump_manifest", "snapshot_source_identity_mismatch")
    if manifest.get("source_snapshot_sha256") != source_snapshot_sha256:
        raise CutoverError("dump_manifest", "source_snapshot_digest_mismatch")
    if manifest.get("source_fence_sha256") != fence_sha256:
        raise CutoverError("dump_manifest", "source_fence_digest_mismatch")
    live = manifest.get("live_session_observations")
    if (
        not isinstance(live, list)
        or len(live) != 2
        or any(
            not isinstance(item, dict)
            or item.get("session_count") != 0
            or not _SHA256_RE.fullmatch(str(item.get("sha256", "")))
            for item in live
        )
    ):
        raise CutoverError("dump_manifest", "invalid_live_session_observations")
    before = _timestamp(live[0].get("captured_at"), "live_before_dump")
    snapshot_at = _timestamp(source_snapshot.get("captured_at"), "source_snapshot")
    after = _timestamp(live[1].get("captured_at"), "live_after_dump")
    created = _timestamp(manifest.get("created_at"), "dump_manifest_created_at")
    if not before <= snapshot_at <= after <= created:
        raise CutoverError("dump_manifest", "invalid_live_evidence_order")
    dump = manifest.get("pg_dump")
    artifact = manifest.get("artifact")
    if (
        not isinstance(dump, dict)
        or dump.get("format") != "custom"
        or dump.get("major_version") != 15
        or not isinstance(artifact, dict)
    ):
        raise CutoverError("dump_manifest", "invalid_dump_metadata")
    path_value = str(manifest.get("artifact_path", ""))
    if not path_value.endswith(".dump"):
        raise CutoverError("dump_manifest", "unsupported_artifact_format")
    descriptor, path, info = _open_held_regular(path_value, "artifact")
    try:
        actual_sha, actual_size = _fd_sha256(descriptor)
        if os.read(descriptor, 5) != b"PGDMP":
            raise CutoverError("dump_manifest", "invalid_custom_archive")
        os.lseek(descriptor, 0, os.SEEK_SET)
        if (
            artifact.get("sha256") != actual_sha
            or artifact.get("size_bytes") != actual_size
            or actual_size != info.st_size
        ):
            raise CutoverError("dump_manifest", "artifact_integrity_mismatch")
        return {
            "fd": descriptor,
            "path": path,
            "sha256": actual_sha,
            "size_bytes": actual_size,
        }
    except Exception:
        os.close(descriptor)
        raise


def _drain(stream: BinaryIO | None, digest: Any, size: list[int]) -> None:
    if stream is None:
        return
    while chunk := stream.read(64 * 1024):
        digest.update(chunk)
        size[0] += len(chunk)


def _create_archive_fd(path_value: str) -> tuple[int, Path]:
    path = Path(path_value)
    if not path.is_absolute() or not path.name.endswith(".dump"):
        raise CutoverError("filesystem", "invalid_archive_output")
    _reject_symlink_components(path.parent)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode):
        raise CutoverError("filesystem", "archive_parent_not_directory")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        return os.open(path, flags, 0o600), path
    except OSError as exc:
        raise CutoverError("filesystem", "archive_create_failed") from exc


def _pg15_version(client: DockerPsql, tool: str) -> str:
    result = subprocess.run(
        [client.docker_binary, "exec", client.container, tool, "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not re.search(
        rf"\b{re.escape(tool)} \(PostgreSQL\) 15(?:\.|\b)", value
    ):
        raise CutoverError("postgres_client", f"{tool}_not_postgresql_15")
    return value


def create_custom_dump(
    client: DockerPsql,
    output: str,
    *,
    timeout_seconds: int,
    serializable_deferrable: bool = False,
    snapshot_id: str | None = None,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> dict[str, Any]:
    if timeout_seconds < 60 or timeout_seconds > 21600:
        raise CutoverError("configuration", "invalid_dump_timeout")
    version = _pg15_version(client, "pg_dump")
    descriptor, path = _create_archive_fd(output)
    app_name = _CUTOVER_APP_PREFIX + "dump"
    process: subprocess.Popen[bytes] | None = None
    try:
        options = ["--format=custom", "--no-owner", "--no-acl"]
        if serializable_deferrable and snapshot_id is not None:
            raise CutoverError("configuration", "conflicting_dump_snapshot_options")
        if serializable_deferrable:
            # Rehearsal dumps are taken while production remains writable.
            # PostgreSQL waits for a safe serializable read-only snapshot, so
            # the resulting archive is transactionally self-consistent without
            # borrowing the final cutover's durable write fence.
            options.append("--serializable-deferrable")
        if snapshot_id is not None:
            if not re.fullmatch(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{8}-[0-9A-Fa-f]+", snapshot_id):
                raise CutoverError("configuration", "invalid_exported_snapshot")
            options.append(f"--snapshot={snapshot_id}")
        process = popen(
            client.pg_tool_argv("pg_dump", *options),
            stdin=subprocess.DEVNULL,
            stdout=descriptor,
            stderr=subprocess.PIPE,
            env=client.environment(app_name=app_name),
        )
        try:
            _stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise CutoverError("dump", "timeout") from exc
        if process.returncode != 0:
            raise CutoverError("dump", "pg_dump_failed")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        held, _held_path, _info = _open_held_regular(str(path), "artifact")
        try:
            digest, size = _fd_sha256(held)
            if os.read(held, 5) != b"PGDMP":
                raise CutoverError("dump", "invalid_custom_archive")
        finally:
            os.close(held)
        return {
            "path": path,
            "sha256": digest,
            "size_bytes": size,
            "client_version": version,
            "stderr_sha256": hashlib.sha256(stderr or b"").hexdigest(),
            "stderr_bytes": len(stderr or b""),
        }
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _restore_backend_count(client: DockerPsql, app_name: str) -> int:
    quoted = "'" + app_name.replace("'", "''") + "'"
    count = client.query_json(
        "SELECT to_jsonb(count(*))::text FROM pg_stat_activity "
        f"WHERE datname=current_database() AND application_name={quoted};"
    )
    if not isinstance(count, int) or count < 0:
        raise CutoverError("restore", "invalid_backend_count")
    return count


def _terminate_restore_backend(client: DockerPsql, app_name: str) -> None:
    quoted = "'" + app_name.replace("'", "''") + "'"
    client.query_json(
        "SELECT to_jsonb(COALESCE(bool_and(pg_terminate_backend(pid)), true))::text "
        "FROM pg_stat_activity WHERE datname=current_database() "
        f"AND application_name={quoted} AND pid<>pg_backend_pid();"
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _restore_backend_count(client, app_name) == 0:
            return
        time.sleep(0.2)
    raise CutoverError("restore", "backend_quarantined")


def stream_restore(
    client: DockerPsql,
    artifact_fd: int,
    *,
    app_name: str,
    timeout_seconds: int,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> dict[str, Any]:
    if timeout_seconds < 60 or timeout_seconds > 21600:
        raise CutoverError("configuration", "invalid_restore_timeout")
    _pg15_version(client, "pg_restore")
    process = popen(
        client.pg_tool_argv(
            "pg_restore",
            "--single-transaction",
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=client.environment(app_name=app_name),
    )
    stdout_hash = hashlib.sha256()
    stderr_hash = hashlib.sha256()
    stdout_size = [0]
    stderr_size = [0]
    errors: list[str] = []
    bytes_sent = [0]
    out_thread = threading.Thread(
        target=_drain, args=(process.stdout, stdout_hash, stdout_size), daemon=True
    )
    err_thread = threading.Thread(
        target=_drain, args=(process.stderr, stderr_hash, stderr_size), daemon=True
    )

    def feed() -> None:
        try:
            os.lseek(artifact_fd, 0, os.SEEK_SET)
            while chunk := os.read(artifact_fd, 1024 * 1024):
                if process.stdin is None:
                    raise BrokenPipeError
                process.stdin.write(chunk)
                bytes_sent[0] += len(chunk)
            if process.stdin is not None:
                process.stdin.close()
        except (BrokenPipeError, EOFError, OSError):
            errors.append("artifact_stream_failed")
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass

    feed_thread = threading.Thread(target=feed, daemon=True)
    started = time.monotonic()
    out_thread.start()
    err_thread.start()
    feed_thread.start()
    while process.poll() is None:
        if time.monotonic() - started >= timeout_seconds:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            _terminate_restore_backend(client, app_name)
            try:
                target_public_schema_empty(client)
            except CutoverError as exc:
                raise CutoverError("restore", "timeout_target_quarantined") from exc
            raise CutoverError("restore", "timeout_rolled_back")
        time.sleep(0.1)
    feed_thread.join(timeout=10)
    out_thread.join(timeout=10)
    err_thread.join(timeout=10)
    if feed_thread.is_alive() or errors:
        _terminate_restore_backend(client, app_name)
        raise CutoverError("restore", "artifact_stream_failed")
    if process.returncode != 0:
        _terminate_restore_backend(client, app_name)
        try:
            target_public_schema_empty(client)
        except CutoverError as exc:
            raise CutoverError("restore", "pg_restore_target_quarantined") from exc
        raise CutoverError("restore", "pg_restore_failed_rolled_back")
    if _restore_backend_count(client, app_name) != 0:
        _terminate_restore_backend(client, app_name)
        raise CutoverError("restore", "success_backend_quarantined")
    return {
        "bytes_sent": bytes_sent[0],
        "stdout_bytes": stdout_size[0],
        "stdout_sha256": stdout_hash.hexdigest(),
        "stderr_bytes": stderr_size[0],
        "stderr_sha256": stderr_hash.hexdigest(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "single_transaction": True,
    }


_FOREIGN_SESSIONS_SQL = f"""
SELECT COALESCE(jsonb_agg(jsonb_build_object(
  'user', usename, 'application_name', application_name,
  'state', state, 'backend_type', backend_type,
  'xact_started', xact_start IS NOT NULL
) ORDER BY pid), '[]'::jsonb)::text
FROM pg_stat_activity
WHERE datname=current_database()
  AND backend_type='client backend'
  AND application_name <> '{_OBSERVER_APP_NAME}';
"""


def _prove_no_foreign_sessions(client: DockerPsql, *, label: str) -> dict[str, Any]:
    sessions = client.query_json(
        _FOREIGN_SESSIONS_SQL, app_name=_OBSERVER_APP_NAME
    )
    if sessions != []:
        raise CutoverError("source_fence", "live_foreign_sessions_present")
    encoded = json.dumps(sessions, separators=(",", ":"), sort_keys=True).encode()
    return {
        "label": label,
        "captured_at": _utcnow(),
        "session_count": 0,
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def dump_source(args: argparse.Namespace) -> dict[str, Any]:
    fence, _fence_path, fence_sha = _load_json(args.source_fence, "source_fence")
    expected = {
        "instance": args.instance,
        "database": args.database,
    }
    source = validate_durable_fence(fence, expected_source=expected)
    client = DockerPsql(
        container=args.container,
        database=args.database,
        user=args.user,
        password_file=args.password_file,
        host=args.host,
        port=args.port,
    )
    live_before = _prove_no_foreign_sessions(client, label="before_snapshot")
    snapshot = snapshot_database(
        client, label="source-frozen-direct-dump", instance=args.instance
    )
    snapshot_path = _atomic_json(args.source_snapshot_output, snapshot)
    snapshot_sha = _file_sha256(snapshot_path)
    archive = create_custom_dump(
        client, args.artifact_output, timeout_seconds=args.timeout_seconds
    )
    live_after = _prove_no_foreign_sessions(client, label="after_dump")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "created_at": _utcnow(),
        "source": source,
        "source_fence_sha256": fence_sha,
        "source_snapshot_sha256": snapshot_sha,
        "live_session_observations": [live_before, live_after],
        "artifact_path": str(archive["path"]),
        "artifact": {
            "sha256": archive["sha256"],
            "size_bytes": archive["size_bytes"],
        },
        "pg_dump": {
            "format": "custom",
            "major_version": 15,
            "client_version": archive["client_version"],
            "no_owner": True,
            "no_acl": True,
            "stderr_sha256": archive["stderr_sha256"],
            "stderr_bytes": archive["stderr_bytes"],
        },
    }
    _atomic_json(args.manifest_output, manifest)
    return manifest


def verify_import(args: argparse.Namespace) -> dict[str, Any]:
    source_snapshot, _source_path, source_sha = _load_json(
        args.source_snapshot, "source_snapshot"
    )
    fence, _fence_path, fence_sha = _load_json(args.source_fence, "source_fence")
    manifest, _manifest_path, manifest_sha = _load_json(
        args.export_manifest, "export_manifest"
    )
    artifact = validate_direct_manifest(
        source_snapshot=source_snapshot,
        source_snapshot_sha256=source_sha,
        fence=fence,
        fence_sha256=fence_sha,
        manifest=manifest,
    )
    target = DockerPsql(
        container=args.target_container,
        database=args.target_database,
        user=args.target_user,
        password_file=args.target_password_file,
        host=args.target_host,
        port=args.target_port,
    )
    empty_inventory = target_public_schema_empty(target)
    app_name = _CUTOVER_APP_PREFIX + "restore-" + hashlib.sha256(
        f"{time.time_ns()}:{os.getpid()}".encode()
    ).hexdigest()[:16]
    try:
        imported = stream_restore(
            target,
            artifact["fd"],
            app_name=app_name,
            timeout_seconds=args.import_timeout_seconds,
        )
        target_snapshot = snapshot_database(
            target, label="target-after-import", instance=args.target_instance
        )
        compare_snapshots(
            source_snapshot,
            target_snapshot,
            source_owner=args.source_owner,
            target_owner=args.target_owner or args.target_user,
        )
        target_path = _atomic_json(args.target_snapshot_output, target_snapshot)
        target_sha = _file_sha256(target_path)
    finally:
        os.close(artifact["fd"])
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": RESULT_KIND,
        "outcome": "success",
        "completed_at": _utcnow(),
        "source_snapshot_sha256": source_sha,
        "source_fence_sha256": fence_sha,
        "export_manifest_sha256": manifest_sha,
        "target_snapshot_sha256": target_sha,
        "target_empty_inventory": empty_inventory,
        "artifact": {
            "sha256": artifact["sha256"],
            "size_bytes": artifact["size_bytes"],
        },
        "import": imported,
        "comparison_fields": list(_COMPARISON_FIELDS),
    }


def _client_from_snapshot_args(args: argparse.Namespace) -> DockerPsql:
    return DockerPsql(
        container=args.container,
        database=args.database,
        user=args.user,
        password_file=args.password_file,
        host=args.host,
        port=args.port,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    capture_fence = commands.add_parser(
        "capture-cloud-run-fence",
        help="Capture a service-bound Cloud Run resource and IAM write-fence proof",
    )
    capture_fence.add_argument("--project-id", required=True)
    capture_fence.add_argument("--region", required=True)
    capture_fence.add_argument("--service", required=True)
    capture_fence.add_argument("--output", required=True)
    capture_fence.add_argument(
        "--gcloud-executable",
        default="gcloud",
        help="gcloud executable or absolute path on the authenticated operator host",
    )

    snapshot = commands.add_parser("snapshot", help="Capture deterministic database evidence")
    snapshot.add_argument("--container", required=True)
    snapshot.add_argument("--database", required=True)
    snapshot.add_argument("--user", required=True)
    snapshot.add_argument("--password-file")
    snapshot.add_argument(
        "--host",
        help="Optional DB host visible inside the container; pair with --port. For the fenced Cloud SQL source, use the authenticated proxy bridge gateway.",
    )
    snapshot.add_argument(
        "--port",
        type=int,
        help="Optional DB port visible inside the container; use 5433 with the standard host auth-proxy bridge.",
    )
    snapshot.add_argument("--label", required=True)
    snapshot.add_argument("--instance", required=True)
    snapshot.add_argument("--output", required=True)

    dump = commands.add_parser(
        "dump-source",
        help="Validate the durable fence and create a PostgreSQL 15 custom archive",
    )
    dump.add_argument("--source-fence", required=True)
    dump.add_argument("--container", required=True)
    dump.add_argument("--database", required=True)
    dump.add_argument("--user", required=True)
    dump.add_argument("--password-file")
    dump.add_argument("--host", required=True)
    dump.add_argument("--port", type=int, required=True)
    dump.add_argument("--instance", required=True)
    dump.add_argument("--artifact-output", required=True)
    dump.add_argument("--source-snapshot-output", required=True)
    dump.add_argument("--manifest-output", required=True)
    dump.add_argument("--timeout-seconds", type=int, default=7200)

    verify = commands.add_parser(
        "verify-import", help="Validate fence/export, import into an empty target, and compare"
    )
    verify.add_argument("--source-snapshot", required=True)
    verify.add_argument("--source-fence", required=True)
    verify.add_argument("--export-manifest", required=True)
    verify.add_argument("--target-container", required=True)
    verify.add_argument("--target-database", required=True)
    verify.add_argument("--target-user", required=True)
    verify.add_argument("--source-owner", default="postgres")
    verify.add_argument("--target-owner")
    verify.add_argument("--target-password-file")
    verify.add_argument("--target-host")
    verify.add_argument("--target-port", type=int)
    verify.add_argument("--target-instance", required=True)
    verify.add_argument("--target-snapshot-output", required=True)
    verify.add_argument("--result-output", required=True)
    verify.add_argument("--import-timeout-seconds", type=int, default=7200)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "capture-cloud-run-fence":
            capture_cloud_run_fence(args)
            print(f"Cloud Run fence evidence written: {args.output}")
            return 0
        if args.command == "snapshot":
            payload = snapshot_database(
                _client_from_snapshot_args(args), label=args.label, instance=args.instance
            )
            _atomic_json(args.output, payload)
            print(f"snapshot evidence written: {args.output}")
            return 0
        if args.command == "dump-source":
            dump_source(args)
            print(f"direct dump manifest written: {args.manifest_output}")
            return 0
        result = verify_import(args)
        _atomic_json(args.result_output, result)
        print(f"cutover import verified: {args.result_output}")
        return 0
    except (CutoverError, OSError) as exc:
        stage = exc.stage if isinstance(exc, CutoverError) else "operating_system"
        code = exc.code if isinstance(exc, CutoverError) else "io_failure"
        result_output = getattr(args, "result_output", None)
        if result_output:
            try:
                _atomic_json(
                    result_output,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "kind": RESULT_KIND,
                        "outcome": "failure",
                        "failed_at": _utcnow(),
                        "stage": stage,
                        "code": code,
                    },
                )
            except (CutoverError, OSError):
                pass
        print(f"ERROR: cutover refused ({stage}:{code})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
