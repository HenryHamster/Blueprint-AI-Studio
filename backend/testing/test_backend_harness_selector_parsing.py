"""Regression coverage for standalone selector ownership in test-backend.sh."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in (Path("/repo"), *here.parents):
        if (candidate / "scripts" / "test-backend.sh").is_file() and (
            candidate / "backend" / "testing"
        ).is_dir():
            return candidate
    raise RuntimeError("could not resolve checkout root for harness selector tests")


REPO_ROOT = _repo_root()


def _bash_executable() -> str | None:
    for candidate in (
        Path("C:/Program Files/Git/bin/bash.exe"),
        Path("C:/Program Files/Git/usr/bin/bash.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return shutil.which("bash")


BASH = _bash_executable()
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")


def _bash_path(path: Path) -> str:
    value = path.resolve().as_posix()
    if os.name == "nt" and len(value) >= 3 and value[1:3] == ":/":
        return f"/{value[0].lower()}/{value[3:]}"
    return value


def _prepare_harness(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    backend = repo / "backend"
    testing = backend / "testing"
    app = backend / "app"
    fake_bin = tmp_path / "bin"
    for directory in (scripts, testing, app, fake_bin):
        directory.mkdir(parents=True, exist_ok=True)

    for name in ("test-backend.sh", "backend-test-route.sh", "posix-path.sh"):
        destination = scripts / name
        shutil.copy2(REPO_ROOT / "scripts" / name, destination)
        destination.chmod(0o755)

    (backend / "requirements.txt").write_text("pytest\nfastapi\n", encoding="utf-8")
    (backend / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (app / "__init__.py").write_text("", encoding="utf-8")
    (app / "main.py").write_text(
        "class _App:\n"
        "    routes = []\n\n"
        "app = _App()\n",
        encoding="utf-8",
    )

    docker = fake_bin / "docker"
    docker.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    docker.chmod(0o755)
    pytest_argv = tmp_path / "pytest-argv.txt"
    python3 = fake_bin / "python3"
    python3.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pytest" ]; then\n'
        '  printf \'%s\\n\' "$@" > "$HARNESS_PYTEST_ARGV"\n'
        '  if [ "${HARNESS_RUN_REAL_PYTEST:-0}" = "1" ]; then\n'
        '    exec "$HARNESS_REAL_PYTHON" "$@"\n'
        "  fi\n"
        "  printf '============================= 1 passed in 0.01s =============================\\n'\n"
        "  exit 0\n"
        "fi\n"
        'exec "$HARNESS_REAL_PYTHON" "$@"\n',
        encoding="utf-8",
    )
    python3.chmod(0o755)

    env = os.environ.copy()
    for name in (
        "BLUEPRINT_STATIC_GUARD_JUNIT",
        "BLUEPRINT_STATIC_GUARD_SELECTORS",
        "BLUEPRINT_VERIFICATION_JUNIT",
        "BLUEPRINT_VERIFICATION_RESULT_FILE",
    ):
        env.pop(name, None)
    env.update(
        {
            "ALLOW_LOW_MEM": "1",
            "BLUEPRINT_BACKEND_TEST_PYTHON": _bash_path(Path(sys.executable)),
            "HARNESS_PYTEST_ARGV": _bash_path(pytest_argv),
            "HARNESS_REAL_PYTHON": _bash_path(Path(sys.executable)),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "TESTMON_NO_FETCH": "1",
        }
    )
    return repo, env, pytest_argv


def _run_harness(
    repo: Path, env: dict[str, str], *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, _bash_path(repo / "scripts" / "test-backend.sh"), *arguments],
        cwd=repo / "backend",
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_standalone_preserves_an_exact_node_selector(tmp_path: Path) -> None:
    repo, env, pytest_argv = _prepare_harness(tmp_path)
    (repo / "backend" / "testing" / "test_selected.py").write_text(
        "def test_selected():\n"
        "    print('EXACT_NODE_RAN')\n\n"
        "def test_sibling_must_not_run():\n"
        "    raise AssertionError('SIBLING_NODE_RAN')\n",
        encoding="utf-8",
    )

    result = _run_harness(
        repo,
        env,
        "--standalone",
        "--no-postgres",
        "testing/test_selected.py::test_selected",
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    argv = pytest_argv.read_text(encoding="utf-8").splitlines()
    assert argv[:3] == [
        "-m",
        "pytest",
        "testing/test_selected.py::test_selected",
    ]
    assert "testing/test_selected.py" not in argv
    assert "1 passed" in output


def test_standalone_selector_does_not_expand_to_default_roster(tmp_path: Path) -> None:
    repo, env, pytest_argv = _prepare_harness(tmp_path)
    (repo / "backend" / "testing" / "test_selected.py").write_text(
        "def test_selected():\n    pass\n", encoding="utf-8"
    )
    (repo / "backend" / "testing" / "test_security.py").write_text(
        "def test_default_roster_must_not_run():\n"
        "    raise AssertionError('DEFAULT_ROSTER_RAN')\n",
        encoding="utf-8",
    )

    result = _run_harness(
        repo,
        env,
        "--standalone",
        "--no-postgres",
        "testing/test_selected.py",
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    argv = pytest_argv.read_text(encoding="utf-8").splitlines()
    assert argv[:3] == ["-m", "pytest", "testing/test_selected.py"]
    assert "testing/test_security.py" not in argv
    assert "1 passed" in output


def test_standalone_quiet_modifier_keeps_default_roster(tmp_path: Path) -> None:
    repo, env, pytest_argv = _prepare_harness(tmp_path)

    result = _run_harness(
        repo,
        env,
        "--standalone",
        "--no-postgres",
        "-q",
        "--maxfail=1",
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    argv = pytest_argv.read_text(encoding="utf-8").splitlines()
    assert "testing/test_security.py" in argv
    assert "testing/test_pipeline.py" in argv
    assert "-q" in argv
    assert "--maxfail=1" in argv


def test_standalone_keyword_modifier_keeps_default_roster(tmp_path: Path) -> None:
    repo, env, pytest_argv = _prepare_harness(tmp_path)

    result = _run_harness(
        repo,
        env,
        "--standalone",
        "--no-postgres",
        "-k",
        "selected_name",
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    argv = pytest_argv.read_text(encoding="utf-8").splitlines()
    assert "testing/test_security.py" in argv
    assert "testing/test_pipeline.py" in argv
    assert argv[argv.index("-k") + 1] == "selected_name"


def test_standalone_rejects_environment_and_positional_selectors(
    tmp_path: Path,
) -> None:
    repo, env, pytest_argv = _prepare_harness(tmp_path)
    for name in ("test_environment.py", "test_positional.py"):
        (repo / "backend" / "testing" / name).write_text(
            "def test_must_not_run():\n"
            "    raise AssertionError('AMBIGUOUS_SELECTOR_RAN')\n",
            encoding="utf-8",
        )
    env["BLUEPRINT_STATIC_GUARD_SELECTORS"] = base64.b64encode(
        b"testing/test_environment.py\n"
    ).decode("ascii")

    result = _run_harness(
        repo,
        env,
        "--standalone",
        "--no-postgres",
        "testing/test_positional.py",
    )

    output = result.stdout + result.stderr
    assert result.returncode == 2, output
    assert "cannot be combined with explicit pytest arguments" in output
    assert not pytest_argv.exists()


@pytest.mark.parametrize(
    "explicit_args",
    [("testing/test_selected.py",), ("-q",)],
)
def test_standalone_inventory_rejects_explicit_arguments_without_output(
    tmp_path: Path, explicit_args: tuple[str, ...]
) -> None:
    repo, env, pytest_argv = _prepare_harness(tmp_path)

    result = _run_harness(
        repo,
        env,
        "--standalone",
        "--standalone-inventory",
        *explicit_args,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert "does not accept explicit pytest arguments or selectors" in result.stderr
    assert "BLUEPRINT_STANDALONE_CORPUS" not in result.stderr
    assert not pytest_argv.exists()


def test_environment_selectors_require_standalone(tmp_path: Path) -> None:
    repo, env, pytest_argv = _prepare_harness(tmp_path)
    env["BLUEPRINT_STATIC_GUARD_SELECTORS"] = base64.b64encode(
        b"testing/test_environment.py\n"
    ).decode("ascii")

    result = _run_harness(repo, env, "--no-postgres")

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert "BLUEPRINT_STATIC_GUARD_SELECTORS requires --standalone" in result.stderr
    assert not pytest_argv.exists()


def test_empty_environment_selector_override_fails_before_pytest(
    tmp_path: Path,
) -> None:
    repo, env, pytest_argv = _prepare_harness(tmp_path)
    env["BLUEPRINT_STATIC_GUARD_SELECTORS"] = ""

    result = _run_harness(repo, env, "--standalone", "--no-postgres")

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert "static guard selector list is empty or invalid" in result.stderr
    assert not pytest_argv.exists()


def test_empty_environment_selector_override_cannot_emit_inventory(
    tmp_path: Path,
) -> None:
    repo, env, pytest_argv = _prepare_harness(tmp_path)
    env["BLUEPRINT_STATIC_GUARD_SELECTORS"] = ""

    result = _run_harness(
        repo,
        env,
        "--standalone",
        "--standalone-inventory",
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert "static guard selector list is empty or invalid" in result.stderr
    assert "BLUEPRINT_STANDALONE_CORPUS" not in result.stderr
    assert not pytest_argv.exists()


@pytest.mark.parametrize(
    "forbidden_entry",
    ["--collect-only", "--setup-only", "--", "--last-failed"],
)
def test_environment_selectors_reject_option_shaped_entries_before_pytest(
    tmp_path: Path, forbidden_entry: str
) -> None:
    repo, env, pytest_argv = _prepare_harness(tmp_path)
    env["BLUEPRINT_STATIC_GUARD_SELECTORS"] = base64.b64encode(
        f"{forbidden_entry}\n".encode()
    ).decode("ascii")

    result = _run_harness(repo, env, "--standalone", "--no-postgres")

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert "must be positional test selectors" in result.stderr
    assert forbidden_entry in result.stderr
    assert not pytest_argv.exists()


@pytest.mark.skipif(
    os.name == "nt",
    reason="the harness's signal timeout method requires POSIX SIGALRM",
)
def test_environment_selector_executes_real_node_body_before_rejecting_mode(
    tmp_path: Path,
) -> None:
    repo, env, pytest_argv = _prepare_harness(tmp_path)
    body_marker = tmp_path / "selected-body-ran.txt"
    (repo / "backend" / "testing" / "test_selected.py").write_text(
        "import os\n"
        "from pathlib import Path\n\n"
        "def test_selected():\n"
        "    Path(os.environ['BODY_MARKER']).write_text('executed', encoding='utf-8')\n\n"
        "def test_sibling_must_not_run():\n"
        "    raise AssertionError('SIBLING_NODE_RAN')\n",
        encoding="utf-8",
    )
    env.update(
        {
            "BODY_MARKER": str(body_marker.resolve()),
            "HARNESS_RUN_REAL_PYTEST": "1",
            "BLUEPRINT_STATIC_GUARD_SELECTORS": base64.b64encode(
                b"testing/test_selected.py::test_selected\n"
            ).decode("ascii"),
        }
    )

    executed = _run_harness(repo, env, "--standalone", "--no-postgres")

    assert executed.returncode == 0, executed.stdout + executed.stderr
    assert body_marker.read_text(encoding="utf-8") == "executed"
    argv = pytest_argv.read_text(encoding="utf-8").splitlines()
    assert "testing/test_selected.py::test_selected" in argv

    body_marker.unlink()
    pytest_argv.unlink()
    env["BLUEPRINT_STATIC_GUARD_SELECTORS"] = base64.b64encode(
        b"--collect-only\n"
    ).decode("ascii")

    rejected = _run_harness(repo, env, "--standalone", "--no-postgres")

    assert rejected.returncode == 2, rejected.stdout + rejected.stderr
    assert rejected.stdout == ""
    assert not body_marker.exists()
    assert not pytest_argv.exists()


@pytest.mark.parametrize(
    "non_execution_mode",
    [
        "--collect-only",
        "--co",
        "--fixtures",
        "--fixtures-per-test",
        "--setup-only",
        "--setup-plan",
    ],
)
def test_standalone_rejects_non_execution_modes(
    tmp_path: Path, non_execution_mode: str
) -> None:
    repo, env, pytest_argv = _prepare_harness(tmp_path)

    result = _run_harness(
        repo,
        env,
        "--standalone",
        "--no-postgres",
        non_execution_mode,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert "must execute test bodies" in result.stderr
    assert not pytest_argv.exists()


def test_standalone_rejects_pytest_option_separator(tmp_path: Path) -> None:
    repo, env, pytest_argv = _prepare_harness(tmp_path)

    result = _run_harness(
        repo,
        env,
        "--standalone",
        "--no-postgres",
        "testing/test_selected.py",
        "--",
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout == ""
    assert "cannot include '--'" in result.stderr
    assert not pytest_argv.exists()


def test_harness_change_selects_parser_regression() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "backend" / "testing" / "impacted_tests.py"),
            "scripts/test-backend.sh",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "backend/testing/test_backend_harness_selector_parsing.py"
        in result.stdout.splitlines()
    )

    harness = (REPO_ROOT / "scripts" / "test-backend.sh").read_text(
        encoding="utf-8"
    )
    wait_block = harness.split('echo "=== Waiting for Postgres ==="', 1)[1].split(
        'echo "=== Checking migration-required PostgreSQL extensions ==="', 1
    )[0]
    normalized_wait = " ".join(wait_block.replace("\\\n", " ").split())

    assert 'docker exec "$PG_CONTAINER" pg_isready' not in wait_block
    assert (
        "psql -X -w -U test -d test_blueprint -v ON_ERROR_STOP=1 "
        "-Atqc 'SELECT 1'" in normalized_wait
    )
    assert "cat /proc/1/comm" in wait_block
    assert '= "postgres" ]' in wait_block
    assert "POSTGRES_READY_STREAK=$((POSTGRES_READY_STREAK + 1))" in wait_block
    assert '[ "$POSTGRES_READY_STREAK" -ge 2 ]' in wait_block
    assert "POSTGRES_READY=1" in wait_block
    assert '[ "$POSTGRES_READY" != "1" ]' in wait_block
    assert "docker logs \"$PG_CONTAINER\"" in wait_block
    assert "exit 1" in wait_block
