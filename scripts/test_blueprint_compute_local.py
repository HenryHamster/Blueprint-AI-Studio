"""Correctness tests for the local `blueprint compute run` path.

These are deliberately NOT argv-shape tests. The defects they pin all survived
a suite of `subprocess.run`-mocked argv assertions, because a mock cannot
observe the thing that was actually broken: on a Windows host the harness
launched a shell that never received the Blueprint file contract, measured
nothing, and reported SUCCEEDED with exit 0.

So the end-to-end cases here run a REAL shell and assert on what the harness
concluded. Mutating any of the three fixes out of blueprint.py turns them red.
"""
import contextlib
import functools
import io
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import blueprint
import blueprint_api
import pytest

_PLAN = blueprint._compute_resolve_shell()
_SHELL_USABLE = _PLAN.exe == "bash" or os.path.isfile(_PLAN.exe)
_needs_shell = pytest.mark.skipif(
    not _SHELL_USABLE,
    reason=f"no usable POSIX shell for the experiment path ({_PLAN.label})",
)


@pytest.fixture(autouse=True)
def _no_ambient_gpu(monkeypatch):
    """GPU detection shells out; keep these tests off the host's hardware."""
    monkeypatch.setattr(
        blueprint,
        "_detect_local_gpu",
        lambda: {"detected": False, "backend": None, "devices": []},
    )


def _spec(tmp_path, **fields) -> str:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    return str(path)


def _run_spec(tmp_path, **fields):
    """Run a real local experiment; return (exit code, captured stdout)."""
    args = blueprint.build_parser().parse_args(
        ["compute", "run", "--local", _spec(tmp_path, **fields)]
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = blueprint._compute_run_local(args)
    return code, buf.getvalue()


def _run_spec_json(tmp_path, **fields):
    """Run a real local experiment in --json mode; return (exit code, result dict).

    The human render is lossy, so the machine-readable result is what the
    environment-record assertions read.
    """
    args = blueprint.build_parser().parse_args(
        ["compute", "run", "--json", "--local", _spec(tmp_path, **fields)]
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = blueprint._compute_run_local(args)
    return code, json.loads(buf.getvalue())


WRITE_ACCURACY = 'printf "%s" \'{"accuracy": 0.9688}\' > "$BLUEPRINT_RESULT_FILE"'

PIP_PROBE = (
    "{ python3 -m pip --version || python -m pip --version || pip --version; } "
    "> /dev/null 2>&1 && " + WRITE_ACCURACY
)


# Literal Windows paths, built without os.sep/os.path.join so the Windows-side
# assertions mean the same thing when this file runs on Linux CI.
BACKSLASH = chr(92)
WINDOWS_DIR = "C:" + BACKSLASH + "Windows"
SYSTEM32_BASH = WINDOWS_DIR + BACKSLASH + "System32" + BACKSLASH + "bash.exe"
GIT_BASH = "C:" + BACKSLASH + "Git" + BACKSLASH + "bin" + BACKSLASH + "bash.exe"
WIN_RESULT_FILE = "C:" + BACKSLASH + "tmp" + BACKSLASH + "bp" + BACKSLASH + "result.json"
WIN_ARTIFACT_DIR = "C:" + BACKSLASH + "tmp" + BACKSLASH + "bp" + BACKSLASH + "artifacts"


# ---------------------------------------------------------------------------
# P0-1: a run that measured nothing can never report SUCCEEDED
# ---------------------------------------------------------------------------


@_needs_shell
def test_declared_metric_never_written_cannot_report_succeeded(tmp_path):
    """The defect verbatim: command exits 0, writes no result file, declares a metric.

    Before the fix this printed `status: SUCCEEDED (exit_code=0)` /
    `metrics: (none emitted)` and returned 0 -- an experiment that measured
    nothing, reported as a success. It is now a FAILED harness result.
    """
    code, out = _run_spec(
        tmp_path,
        command="echo 'training done, forgot to write metrics'",
        declared_metric="accuracy",
    )
    assert "SUCCEEDED" not in out, f"a run that measured nothing reported success:\n{out}"
    assert "status      : FAILED" in out, out
    assert code == blueprint._COMPUTE_EXIT_HARNESS_FAILED, out


@_needs_shell
def test_criterion_declared_but_never_measured_cannot_report_succeeded(tmp_path):
    """A success_criterion with no metric to evaluate is also an unmeasured run."""
    code, out = _run_spec(
        tmp_path,
        command="true",
        success_criterion={"metric": "accuracy", "operator": ">=", "threshold": 0.9},
    )
    assert "SUCCEEDED" not in out, out
    assert code == blueprint._COMPUTE_EXIT_HARNESS_FAILED, out


@_needs_shell
def test_result_file_written_but_declared_metric_absent_is_failed(tmp_path):
    """A result file that omits the declared metric measured nothing that was asked for."""
    code, out = _run_spec(
        tmp_path,
        command='printf "%s" \'{"loss": 0.2}\' > "$BLUEPRINT_RESULT_FILE"',
        declared_metric="accuracy",
    )
    assert "SUCCEEDED" not in out, out
    assert code == blueprint._COMPUTE_EXIT_HARNESS_FAILED, out


@_needs_shell
def test_declared_metric_actually_captured_reports_succeeded(tmp_path):
    """Positive control: the contract reaches the shell and the metric comes back.

    This is the half of the fix a "fail loudly" shortcut would have broken --
    on Windows this only passes because the contract is translated into the
    WSL shell rather than assumed to be inherited.
    """
    code, out = _run_spec(
        tmp_path,
        command=WRITE_ACCURACY,
        declared_metric="accuracy",
        success_criterion={"metric": "accuracy", "operator": ">=", "threshold": 0.9},
    )
    assert "status      : SUCCEEDED" in out, out
    assert "0.9688" in out, out
    assert "verdict     : MET" in out, out
    assert code == blueprint._COMPUTE_EXIT_OK, out


@_needs_shell
def test_artifact_dir_reaches_the_experiment_shell(tmp_path):
    """BLUEPRINT_ARTIFACT_DIR must be writable from inside the shell, not just set."""
    code, out = _run_spec(
        tmp_path,
        command=(
            'echo hi > "$BLUEPRINT_ARTIFACT_DIR/probe.txt" && '
            'printf "%s" \'{"ok": 1}\' > "$BLUEPRINT_RESULT_FILE"'
        ),
        declared_metric="ok",
    )
    assert code == blueprint._COMPUTE_EXIT_OK, out


# ---------------------------------------------------------------------------
# P0-1 root cause: the shell is resolved, never left to CreateProcess
# ---------------------------------------------------------------------------


def test_resolve_shell_never_returns_bare_bash_on_windows(tmp_path):
    """On Windows the bare name IS the bug, so it must never be what we launch.

    CreateProcess searches System32 before PATH, so argv[0] == "bash" starts
    the WSL launcher even when shutil.which("bash") reports Git Bash. Every
    Windows branch must therefore name an absolute interpreter.
    """
    gitbash = str(tmp_path / "Git" / "bin" / "bash.exe")
    plan = blueprint._compute_resolve_shell(
        _name="nt", _isfile=lambda p: False, _which=lambda n: gitbash
    )
    assert plan.exe == gitbash
    assert plan.exe != "bash"


def test_resolve_shell_flags_system32_bash_as_wsl(monkeypatch):
    """System32 bash.exe is the WSL launcher and must be planned for as WSL."""
    monkeypatch.setenv("SystemRoot", WINDOWS_DIR)
    plan = blueprint._compute_resolve_shell(
        _name="nt",
        _isfile=lambda p: p == SYSTEM32_BASH,
        # which() reporting Git Bash must NOT win: it is not what would run.
        _which=lambda n: GIT_BASH,
    )
    assert plan.exe == SYSTEM32_BASH
    assert plan.is_wsl, plan
    assert plan.kind == blueprint._COMPUTE_SHELL_WSL


def test_resolve_shell_on_posix_is_plain_bash():
    plan = blueprint._compute_resolve_shell(_name="posix")
    assert plan == blueprint._ShellPlan("bash", blueprint._COMPUTE_SHELL_POSIX, "bash")
    assert not plan.is_wsl


def test_windows_paths_translate_to_the_wsl_mount_view():
    """Pinned with literal Windows paths so the answer is host-independent."""
    assert blueprint._windows_path_to_wsl(WIN_RESULT_FILE) == "/mnt/c/tmp/bp/result.json"
    assert blueprint._windows_path_to_wsl(WIN_ARTIFACT_DIR) == "/mnt/c/tmp/bp/artifacts"
    assert blueprint._windows_path_to_wsl("D:" + BACKSLASH) == "/mnt/d"
    # Already-POSIX input is left alone rather than corrupted.
    assert blueprint._windows_path_to_wsl("/tmp/x/r.json").endswith("/tmp/x/r.json")


def _wsl_plan():
    return blueprint._ShellPlan(SYSTEM32_BASH, blueprint._COMPUTE_SHELL_WSL, "WSL bash")


def test_wsl_program_carries_the_contract_as_translated_exports():
    """WSL does not inherit the Win32 environment block, so env= alone is a lie."""
    program = blueprint._compute_shell_command(
        _wsl_plan(),
        "python3 train.py",
        {
            "BLUEPRINT_RESULT_FILE": WIN_RESULT_FILE,
            "BLUEPRINT_ARTIFACT_DIR": WIN_ARTIFACT_DIR,
            "BLUEPRINT_LOCAL_GPU": "none",
        },
    )
    assert "export BLUEPRINT_RESULT_FILE=/mnt/c/tmp/bp/result.json" in program
    assert "export BLUEPRINT_ARTIFACT_DIR=/mnt/c/tmp/bp/artifacts" in program
    assert "export BLUEPRINT_LOCAL_GPU=none" in program
    # A Windows path must never be handed to the Linux shell.
    assert "C:" not in program


def test_posix_program_injects_no_exports_but_keeps_the_guard():
    program = blueprint._compute_shell_command(
        blueprint._ShellPlan("bash", blueprint._COMPUTE_SHELL_POSIX, "bash"),
        "python3 train.py",
        {"BLUEPRINT_RESULT_FILE": "/tmp/r.json"},
    )
    assert "export BLUEPRINT_RESULT_FILE" not in program
    assert "BLUEPRINT_RESULT_FILE did not reach the experiment shell" in program
    assert program.endswith("python3 train.py")


def test_wsl_invocation_uses_a_script_file_not_an_inline_c_string(tmp_path):
    """The WSL launcher's -c string is expanded ONE extra time before bash parses it.

    An inline program therefore loses every variable it sets -- including
    inside single quotes -- so the contract must travel as a real script file.
    """
    script = tmp_path / "run.sh"
    plan = _wsl_plan()
    argv = blueprint._compute_shell_invocation(
        plan,
        'echo "$BLUEPRINT_RESULT_FILE"',
        {"BLUEPRINT_RESULT_FILE": WIN_RESULT_FILE},
        script_path=str(script),
    )
    assert "-c" not in argv and "-lc" not in argv, argv
    assert argv[0] == plan.exe
    assert argv[1] == "-l"
    assert argv[2] == blueprint._windows_path_to_wsl(str(script)), argv
    body = script.read_bytes()
    assert b"export BLUEPRINT_RESULT_FILE=/mnt/c/tmp/bp/result.json" in body
    # Written with LF: a CRLF script is not runnable by a Linux shell.
    assert b"\r\n" not in body, body


def test_posix_invocation_stays_inline(tmp_path):
    script = tmp_path / "unused.sh"
    argv = blueprint._compute_shell_invocation(
        blueprint._ShellPlan("bash", blueprint._COMPUTE_SHELL_POSIX, "bash"),
        "true",
        {"BLUEPRINT_RESULT_FILE": "/tmp/r.json"},
        script_path=str(script),
    )
    assert argv[:2] == ["bash", "-lc"]
    assert not script.exists(), "POSIX must not need a scratch script file"


@_needs_shell
def test_contract_prologue_fails_loudly_when_the_contract_is_missing(tmp_path):
    """Run the guard in a real shell with the contract stripped."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("BLUEPRINT_")}
    if _PLAN.is_wsl:
        argv = blueprint._compute_shell_invocation(
            _PLAN, "echo SHOULD_NOT_RUN", {}, script_path=str(tmp_path / "guard.sh")
        )
    else:
        program = blueprint._COMPUTE_CONTRACT_PROLOGUE + "\necho SHOULD_NOT_RUN\n"
        argv = [_PLAN.exe, "-lc", program]
    proc = subprocess.run(argv, env=env, capture_output=True)
    assert proc.returncode == blueprint._COMPUTE_CONTRACT_BREACH_EXIT, proc
    assert b"SHOULD_NOT_RUN" not in proc.stdout, proc.stdout
    assert b"did not reach the experiment shell" in proc.stderr, proc.stderr


def test_contract_breach_is_reported_as_a_named_failure():
    reason = blueprint._compute_arm_failure_reason(
        blueprint._COMPUTE_CONTRACT_BREACH_EXIT,
        "accuracy",
        result_missing=True,
        metric_missing=False,
        subject="command",
    )
    assert "never received the Blueprint file contract" in reason
    assert (
        blueprint._compute_run_exit_code(
            exit_code=blueprint._COMPUTE_CONTRACT_BREACH_EXIT,
            measurement_failed=True,
            verdict=None,
        )
        == blueprint._COMPUTE_EXIT_HARNESS_FAILED
    )


# ---------------------------------------------------------------------------
# P0-2: a credential-free verb resolves no credential
# ---------------------------------------------------------------------------


def test_compute_run_is_declared_local_only():
    assert ("compute", "run") in blueprint._LOCAL_ONLY_CLI_COMMANDS
    args = blueprint.build_parser().parse_args(["compute", "run", "true"])
    assert blueprint._is_local_only_cli_command(args)


@_needs_shell
def test_local_only_verb_survives_a_missing_api_config(monkeypatch, tmp_path):
    """The exact P0-2 failure: a purely local run died on a missing API config.

    The contract this pins is "never DIE on a missing config", not "never READ
    the config". The distinction matters: the scoped-credential fence has to
    read the config to know whether a scoped credential is even selected, and
    an earlier version of this test asserted the stronger "never touched"
    property, which is what licensed `compute run` to skip the fence entirely
    and let a project-operator credential run a host command (see
    ``test_local_only_verb_is_still_fenced_for_a_scoped_credential``).

    So load_config() is replaced with the real missing-config behaviour -- it
    raises APIError, exactly as blueprint_api does after printing the
    `gcloud auth login` hint -- on BOTH module bindings. blueprint.py does
    `from blueprint_api import load_config`, so patching only blueprint_api
    left blueprint's own binding live and the old landmine never fired there.
    """

    def _no_config(*_a, **_k):
        raise blueprint_api.APIError("no Blueprint API config found")

    monkeypatch.setattr(blueprint_api, "load_config", _no_config)
    monkeypatch.setattr(blueprint, "load_config", _no_config)
    monkeypatch.setattr(blueprint_api, "_config_cache", None)
    monkeypatch.delenv("BLUEPRINT_CONFIG", raising=False)
    monkeypatch.delenv("BLUEPRINT_API_URL", raising=False)
    monkeypatch.delenv("BLUEPRINT_API_KEY", raising=False)

    spec = _spec(tmp_path, command=WRITE_ACCURACY, declared_metric="accuracy")
    assert blueprint.main(["compute", "run", "--local", spec]) == blueprint._COMPUTE_EXIT_OK


def test_local_only_verb_resolves_no_profile_without_a_scoped_credential(monkeypatch):
    """An ordinary (non-scoped) host runs `compute run` with no profile lookup."""

    def _landmine():
        raise AssertionError("scoped profile resolved without a scoped credential")

    monkeypatch.setattr(blueprint, "_using_scoped_service_credential", lambda: False)
    monkeypatch.setattr(blueprint, "_scoped_service_profile", _landmine)
    args = blueprint.build_parser().parse_args(["compute", "run", "true"])
    blueprint._enforce_scoped_cli_command(args)  # must not raise


def test_local_only_verb_is_still_fenced_for_a_scoped_credential(monkeypatch, capsys):
    """SECURITY PIN: local-only is not a fence exemption.

    `compute` is a host command. CLAUDE.md requires the project-operator
    profile to fail closed on platform/host commands, and ("compute", "run") is
    deliberately absent from both allowlists. The credential-free contract for
    local verbs must therefore be scoped to "no credential is configured" --
    never to "skip the fence". An earlier ordering took the local-only
    exemption first, so a scoped credential reached `cmd_compute` unfenced;
    scripts/test_hermes_operator_cli.py caught it in the scripts test lane.
    """
    monkeypatch.setattr(blueprint, "_using_scoped_service_credential", lambda: True)
    monkeypatch.setattr(
        blueprint,
        "_scoped_service_profile",
        lambda: {"credential": {"scopes": ["project-operator"]}},
    )
    args = blueprint.build_parser().parse_args(["compute", "run", "true"])
    with pytest.raises(blueprint_api.APIError):
        blueprint._enforce_scoped_cli_command(args)
    assert "scoped ServiceCredential command not permitted" in capsys.readouterr().err


def test_missing_config_does_not_wedge_the_fence(monkeypatch):
    """An unresolvable config is waved through: there is no credential to scope."""

    def _no_config():
        raise blueprint_api.APIError("no Blueprint API config found")

    monkeypatch.setattr(blueprint, "_using_scoped_service_credential", _no_config)
    args = blueprint.build_parser().parse_args(["compute", "run", "true"])
    blueprint._enforce_scoped_cli_command(args)  # must not raise


def test_the_fence_is_not_weakened_for_api_touching_verbs(monkeypatch):
    """Scoping fix, not a removal: every other verb still hits the fence."""
    monkeypatch.setattr(blueprint, "_using_scoped_service_credential", lambda: True)
    monkeypatch.setattr(
        blueprint,
        "_scoped_service_profile",
        lambda: {"credential": {"scopes": ["coding-agent:read"]}},
    )
    denied = blueprint.build_parser().parse_args(["compute", "launch", "--dud", "--yes"])
    assert not blueprint._is_local_only_cli_command(denied)
    with pytest.raises(blueprint_api.APIError):
        blueprint._enforce_scoped_cli_command(denied)


def test_local_only_set_stays_minimal_and_disjoint_from_the_allowlists():
    """Guard against the exemption quietly growing to cover an API verb."""
    assert blueprint._LOCAL_ONLY_CLI_COMMANDS == frozenset({("compute", "run")})
    assert not blueprint._LOCAL_ONLY_CLI_COMMANDS & set(blueprint._SCOPED_READ_COMMANDS)
    assert not blueprint._LOCAL_ONLY_CLI_COMMANDS & set(
        blueprint._PROJECT_OPERATOR_WRITE_COMMANDS
    )


# ---------------------------------------------------------------------------
# P0-3: truthful exit codes
# ---------------------------------------------------------------------------


def test_exit_code_contract_distinguishes_the_three_failure_kinds():
    ec = blueprint._compute_run_exit_code
    met = {"verdict": "MET"}
    not_met = {"verdict": "NOT MET"}
    not_evaluable = {"verdict": "NOT EVALUABLE"}
    assert ec(exit_code=0, measurement_failed=False, verdict=None) == 0
    assert ec(exit_code=0, measurement_failed=False, verdict=met) == 0
    assert ec(exit_code=3, measurement_failed=True, verdict=None) == 1
    assert ec(exit_code=0, measurement_failed=True, verdict=None) == 2
    assert ec(exit_code=0, measurement_failed=False, verdict=not_met) == 3
    assert ec(exit_code=0, measurement_failed=False, verdict=not_evaluable) == 2
    # Literal pins, so a constant rename cannot silently redefine the contract.
    assert blueprint._COMPUTE_EXIT_OK == 0
    assert blueprint._COMPUTE_EXIT_COMMAND_FAILED == 1
    assert blueprint._COMPUTE_EXIT_HARNESS_FAILED == 2
    assert blueprint._COMPUTE_EXIT_CRITERION_NOT_MET == 3


def test_exit_codes_are_documented_in_the_command_help():
    """A convention nobody can read is not a convention."""
    help_text = blueprint._COMPUTE_RUN_EXIT_CODE_HELP
    for code in ("0", "1", "2", "3"):
        assert f"  {code}  " in help_text, help_text
    parser = blueprint.build_parser()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        parser.parse_args(["compute", "run", "--help"])
    assert "exit codes:" in buf.getvalue()


@_needs_shell
def test_command_failure_exits_one_not_zero(tmp_path):
    """Measured before the fix: a command that exited 3 made `compute run` exit 0."""
    code, out = _run_spec(
        tmp_path, command="echo boom >&2; exit 3", declared_metric="accuracy"
    )
    assert code == blueprint._COMPUTE_EXIT_COMMAND_FAILED, out
    assert "status      : FAILED (exit_code=3)" in out, out


@_needs_shell
def test_criterion_not_met_exits_three_not_zero(tmp_path):
    """Measured before the fix: a NOT MET criterion still exited 0."""
    code, out = _run_spec(
        tmp_path,
        command='printf "%s" \'{"accuracy": 0.42}\' > "$BLUEPRINT_RESULT_FILE"',
        declared_metric="accuracy",
        success_criterion={"metric": "accuracy", "operator": ">=", "threshold": 0.9},
    )
    assert "verdict     : NOT MET" in out, out
    assert code == blueprint._COMPUTE_EXIT_CRITERION_NOT_MET, out


def test_bad_spec_is_a_harness_failure(tmp_path):
    bad = tmp_path / "spec.json"
    bad.write_text("{not json", encoding="utf-8")
    args = blueprint.build_parser().parse_args(["compute", "run", "--local", str(bad)])
    with pytest.raises(SystemExit) as exc:
        blueprint._compute_run_local(args)
    assert exc.value.code == blueprint._COMPUTE_EXIT_HARNESS_FAILED


# ---------------------------------------------------------------------------
# P1: arms verdict, per-arm artifacts, single-command diagnostics
# ---------------------------------------------------------------------------


def _arm(name, accuracy):
    return {
        "name": name,
        "command": 'printf "%%s" \'{"accuracy": %s}\' > "$BLUEPRINT_RESULT_FILE"' % accuracy,
    }


@_needs_shell
def test_arms_evaluate_the_success_criterion(tmp_path):
    """success_criterion used to be parsed and then silently dropped on the arms path."""
    code, out = _run_spec(
        tmp_path,
        arms=[_arm("lr 0.01", "0.71"), _arm("lr 0.1", "0.93")],
        declared_metric="accuracy",
        success_criterion={"metric": "accuracy", "operator": ">=", "threshold": 0.9},
    )
    assert "verdict     : MET" in out, out
    assert "lr 0.1" in out, out
    assert code == blueprint._COMPUTE_EXIT_OK, out


@_needs_shell
def test_arms_criterion_not_met_across_every_arm_exits_three(tmp_path):
    code, out = _run_spec(
        tmp_path,
        arms=[_arm("a", "0.1"), _arm("b", "0.2")],
        declared_metric="accuracy",
        success_criterion={"metric": "accuracy", "operator": ">=", "threshold": 0.9},
    )
    assert "verdict     : NOT MET" in out, out
    assert code == blueprint._COMPUTE_EXIT_CRITERION_NOT_MET, out


@_needs_shell
def test_each_arm_gets_its_own_artifact_directory(tmp_path):
    """Arms shared one BLUEPRINT_ARTIFACT_DIR, so same-named outputs clobbered.

    Both halves of the isolation are pinned in one run: every arm reports the
    directory it was given (they must differ), and each arm refuses to start if
    a previous arm's identically-named artifact is already sitting there -- the
    clobber itself, which a sequential sweep otherwise hides because each arm
    reads back its own fresh overwrite.
    """
    collide = (
        '[ -e "$BLUEPRINT_ARTIFACT_DIR/log.txt" ] && '
        '{ echo "another arm already wrote log.txt here" >&2; exit 9; }; '
        'echo x > "$BLUEPRINT_ARTIFACT_DIR/log.txt"; '
        'printf \'{"dir": "%s"}\' "$BLUEPRINT_ARTIFACT_DIR" > "$BLUEPRINT_RESULT_FILE"'
    )
    args = blueprint.build_parser().parse_args(
        [
            "compute",
            "run",
            "--json",
            "--local",
            _spec(
                tmp_path,
                arms=[
                    {"name": "a", "command": collide},
                    {"name": "b", "command": collide},
                ],
                declared_metric="dir",
            ),
        ]
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = blueprint._compute_run_local(args)
    payload = json.loads(buf.getvalue())
    arms = payload["metrics"]["arms"]
    dirs = {name: entry.get("dir") for name, entry in arms.items()}
    assert code == blueprint._COMPUTE_EXIT_OK, payload
    assert None not in dirs.values(), payload
    assert dirs["a"] != dirs["b"], payload


def test_arm_slug_is_filesystem_safe():
    assert blueprint._compute_arm_slug("lr 0.01") == "lr_0.01"
    assert blueprint._compute_arm_slug("a/b:c") == "a_b_c"
    assert blueprint._compute_arm_slug("lr 0.01") != blueprint._compute_arm_slug("lr 0.1")


@_needs_shell
def test_arms_that_fail_are_never_reported_succeeded(tmp_path):
    code, out = _run_spec(
        tmp_path,
        arms=[_arm("ok", "1"), {"name": "silent", "command": "true"}],
        declared_metric="accuracy",
    )
    assert "status      : SUCCEEDED" not in out, out
    assert code == blueprint._COMPUTE_EXIT_HARNESS_FAILED, out


@_needs_shell
def test_single_command_failure_carries_a_reason_and_stderr(tmp_path):
    """The single-command path used to print no diagnostics at all."""
    code, out = _run_spec(
        tmp_path,
        command="echo 'CUDA out of memory' >&2; exit 1",
        declared_metric="accuracy",
    )
    assert "error       :" in out, out
    assert "CUDA out of memory" in out, out
    assert code == blueprint._COMPUTE_EXIT_COMMAND_FAILED, out


def test_module_is_importable_without_a_blueprint_config(monkeypatch):
    """Regression fence for the import-time half of the credential-gate defect."""
    monkeypatch.delenv("BLUEPRINT_CONFIG", raising=False)
    assert Path(blueprint.__file__).name == "blueprint.py"
    assert sys.modules["blueprint"] is blueprint


# ---------------------------------------------------------------------------
# P1-1: a LOCALLY BUILT image is usable
#
# `_compute_run_local` used to `docker pull` unconditionally and exit 2 when
# the pull failed. A tag built on this machine exists in no registry, so it
# could never be pulled and therefore could never be run -- which made "clean
# images on local hardware" impossible. Verbatim, before the fix:
#
#   image       : blueprint-compute-selftest:manual
#   docker pull : pulling...
#   error: docker pull 'blueprint-compute-selftest:manual' failed (exit 1)
#   EXITCODE=2
#
# MOCKED tests below pin the policy; the REAL ones at the bottom of this file
# build an actual image and run an actual experiment in it, because the whole
# reason nobody noticed this defect is that every pre-existing container test
# was a subprocess-mocked argv assertion.
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


_LOCAL_IDENTITY = {
    "id": "sha256:" + "a" * 64,
    "repo_digests": ["ghcr.io/acme/trainer@sha256:" + "b" * 64],
}


def _resolve_with(monkeypatch, *, local, pull_returncode=0, **kwargs):
    """Resolve an image with docker replaced; return (block, calls)."""
    calls: list[list[str]] = []

    def fake_run(argv, **_kw):
        calls.append(list(argv))
        if argv[:2] == ["docker", "pull"]:
            return _FakeProc(pull_returncode)
        return _FakeProc(0)

    def fake_inspect(image, **_kw):
        return dict(local) if local is not None else None

    block = blueprint._compute_resolve_image(
        kwargs.pop("image", "ghcr.io/acme/trainer:v3"),
        _subprocess_run=fake_run,
        _inspect=fake_inspect,
        quiet=True,
        **kwargs,
    )
    return block, calls


def test_a_locally_present_image_is_run_without_any_pull(monkeypatch):
    """THE FIX. An image already on this host is used as-is."""
    block, calls = _resolve_with(monkeypatch, local=_LOCAL_IDENTITY)
    assert block["source"] == "local"
    assert "error" not in block, block
    assert not any(c[:2] == ["docker", "pull"] for c in calls), calls


def test_a_failed_pull_is_fatal_only_when_no_local_image_exists(monkeypatch):
    """A locally built tag fails to pull BY DEFINITION; that must not end the run."""
    kept, _ = _resolve_with(monkeypatch, local=_LOCAL_IDENTITY,
                            pull_returncode=1, policy="always")
    assert "error" not in kept, kept
    assert kept["source"] == "local"
    assert kept["pull_failed_exit_code"] == 1

    nothing, _ = _resolve_with(monkeypatch, local=None, pull_returncode=1)
    assert "no local copy of the image exists" in nothing["error"], nothing


def test_pull_missing_fetches_only_when_the_image_is_absent(monkeypatch):
    _, calls = _resolve_with(monkeypatch, local=None)
    assert ["docker", "pull", "ghcr.io/acme/trainer:v3"] in calls, calls


def test_pull_always_refreshes_an_image_that_is_already_here(monkeypatch):
    _, calls = _resolve_with(monkeypatch, local=_LOCAL_IDENTITY, policy="always")
    assert ["docker", "pull", "ghcr.io/acme/trainer:v3"] in calls, calls


def test_pull_never_refuses_instead_of_reaching_the_network(monkeypatch):
    block, calls = _resolve_with(monkeypatch, local=None, policy="never")
    assert "--pull never" in block["error"], block
    assert not any(c[:2] == ["docker", "pull"] for c in calls), calls


def test_the_default_pull_policy_is_the_working_one():
    """No flag may default to the behaviour that rejected a local image."""
    args = blueprint.build_parser().parse_args(["compute", "run", "echo hi"])
    assert args.pull_policy == "missing"
    assert blueprint._COMPUTE_PULL_POLICIES == ("missing", "always", "never")


def test_registry_login_is_attempted_only_when_a_pull_will_happen(monkeypatch):
    """A credential-free local image must not drag in a docker login."""
    logins: list[dict] = []

    def fake_login(auth, **_kw):
        logins.append(auth)
        return True

    blueprint._compute_resolve_image(
        "ghcr.io/acme/trainer:v3",
        registry_auth={"password": "s3cret"},
        quiet=True,
        _subprocess_run=lambda *a, **k: _FakeProc(0),
        _inspect=lambda image, **kw: dict(_LOCAL_IDENTITY),
        _login=fake_login,
    )
    assert logins == [], "a local image needs no registry credential"


# ---------------------------------------------------------------------------
# P1-2: setup_step is honoured, and no other spec field is dropped in silence
# ---------------------------------------------------------------------------


@_needs_shell
def test_setup_step_runs_before_the_command_in_the_same_shell(tmp_path):
    """The defect verbatim: setup_step was parsed by the cloud schema and dropped here.

    The command can only produce this metric if the setup step ran first AND
    its exported variable survived into the command's shell.
    """
    code, out = _run_spec(
        tmp_path,
        setup_step="BP_SETUP_ACCURACY=0.8125",
        command='printf "%s" "{\\"accuracy\\": $BP_SETUP_ACCURACY}" > "$BLUEPRINT_RESULT_FILE"',
        declared_metric="accuracy",
        success_criterion={"metric": "accuracy", "operator": ">=", "threshold": 0.8},
    )
    assert "0.8125" in out, out
    assert "verdict     : MET" in out, out
    assert code == blueprint._COMPUTE_EXIT_OK, out


@_needs_shell
def test_a_failing_setup_step_aborts_before_the_command(tmp_path):
    """`&&` between the brace groups: a broken environment never runs the experiment."""
    marker = tmp_path / "command_ran"
    code, out = _run_spec(
        tmp_path,
        setup_step="exit 3",
        command=f'touch "{marker.as_posix()}"; printf "%s" \'{{"accuracy": 1}}\' > "$BLUEPRINT_RESULT_FILE"',
        declared_metric="accuracy",
    )
    assert not marker.exists(), "the command ran despite a failed setup step"
    assert code == blueprint._COMPUTE_EXIT_COMMAND_FAILED, out
    assert "status      : FAILED" in out, out


def test_setup_step_folds_with_the_command_the_way_the_runner_does():
    folded = blueprint._compute_fold_setup_step("train.py", "pip install -r r.txt")
    assert folded == "{\npip install -r r.txt\n} && {\ntrain.py\n}"
    assert blueprint._compute_fold_setup_step("train.py", None) == "train.py"


@_needs_shell
def test_setup_step_reaches_every_arm_of_a_sweep(tmp_path):
    code, out = _run_spec(
        tmp_path,
        setup_step="BP_BASE=0.5",
        arms=[
            {"name": "a", "command":
                'printf "%s" "{\\"score\\": $BP_BASE}" > "$BLUEPRINT_RESULT_FILE"'},
            {"name": "b", "setup_step": "BP_BASE=0.9", "command":
                'printf "%s" "{\\"score\\": $BP_BASE}" > "$BLUEPRINT_RESULT_FILE"'},
        ],
        declared_metric="score",
    )
    assert "[a] score=0.5" in out, out
    assert "[b] score=0.9" in out, out
    assert code == blueprint._COMPUTE_EXIT_OK, out


def test_a_spec_field_that_changes_the_measurement_is_refused_by_name(tmp_path, capsys):
    """Silently ignoring a declared measurement is the defect class, not a courtesy."""
    args = blueprint.build_parser().parse_args(
        ["compute", "run", "--local",
         _spec(tmp_path, command="true",
               success_criteria=[{"metric": "acc", "operator": ">=", "threshold": 1}])]
    )
    with pytest.raises(SystemExit) as exc:
        blueprint._compute_run_local(args)
    assert exc.value.code == blueprint._COMPUTE_EXIT_HARNESS_FAILED
    assert "'success_criteria'" in capsys.readouterr().err


def test_every_unhonoured_spec_field_is_either_fatal_or_named(tmp_path):
    fatal, ignored = blueprint._compute_spec_field_report({
        "command": "true",
        "setup_step": "true",
        "machine_type": "a2-highgpu-1g",
        "input_artifact_job_id": "cjob-1",
        "totally_unknown": 1,
    })
    assert fatal == ["input_artifact_job_id"]
    assert ignored == ["machine_type", "totally_unknown"]
    assert "CLOUD launch" in blueprint._compute_spec_ignored_warning("machine_type")
    assert "was ignored" in blueprint._compute_spec_ignored_warning("totally_unknown")
    # The two sets must not overlap, or a field would be both honoured and warned about.
    assert not (blueprint._COMPUTE_LOCAL_SPEC_HONOURED
                & blueprint._COMPUTE_LOCAL_SPEC_CLOUD_ONLY)
    assert not (blueprint._COMPUTE_LOCAL_SPEC_HONOURED
                & set(blueprint._COMPUTE_LOCAL_SPEC_UNSUPPORTED))


@_needs_shell
def test_an_ignored_cloud_field_is_reported_on_stderr_and_in_the_result(tmp_path, capsys):
    code, result = _run_spec_json(
        tmp_path,
        command=WRITE_ACCURACY,
        declared_metric="accuracy",
        machine_type="a2-highgpu-1g",
    )
    assert code == blueprint._COMPUTE_EXIT_OK, result
    assert any("machine_type" in w for w in result["spec_warnings"]), result
    assert "machine_type" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# P1-3: repo_clone -- the only way to get code into the container
# ---------------------------------------------------------------------------


def _container_argv(monkeypatch, tmp_path, **fields):
    """Run the container path with docker replaced; return (argv, result)."""
    captured: dict = {}

    monkeypatch.setattr(
        blueprint, "_compute_resolve_image",
        lambda image, **kw: {
            "reference": image, "source": "local", "pull_policy": kw.get("policy"),
            "digest": "sha256:" + "c" * 64, "image_id": "sha256:" + "c" * 64,
            "repo_digests": [], "digest_pinned_reference": False,
            "reproduce_reference": "sha256:" + "c" * 64,
        },
    )

    def fake_run(argv, **_kw):
        captured["argv"] = list(argv)
        mount = next(
            (argv[i + 1] for i, a in enumerate(argv)
             if a == "-v" and argv[i + 1].endswith(":/tmp/blueprint_out")),
            None,
        )
        if mount:
            host_dir = mount[: -len(":/tmp/blueprint_out")]
            Path(host_dir, "result.json").write_text('{"accuracy": 0.5}', encoding="utf-8")
        return _FakeProc(0)

    monkeypatch.setattr(blueprint, "_run_capturing_stderr", fake_run)
    code, result = _run_spec_json(tmp_path, **fields)
    return captured.get("argv", []), code, result


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "-A"],
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"],
    ):
        subprocess.run(argv, cwd=str(root), capture_output=True, timeout=10, check=False)


def test_repo_clone_mounts_the_repository_root_not_the_current_directory(
    monkeypatch, tmp_path
):
    """`os.path.abspath('.')` mounted whatever directory you happened to be in."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    _init_repo(repo)
    monkeypatch.chdir(repo / "pkg")

    argv, code, result = _container_argv(
        monkeypatch, tmp_path,
        container_image="ghcr.io/acme/trainer:v3",
        command="true",
        repo_clone={"owner": "acme", "repo": "trainer", "ref": "main"},
    )
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    workspace = [m for m in mounts if m.endswith(":/workspace")]
    assert workspace, argv
    host = workspace[0][: -len(":/workspace")]
    assert Path(host).resolve() == repo.resolve(), (host, str(repo))
    assert "PYTHONPATH=/workspace" in argv, argv
    assert argv[argv.index("-w") + 1] == "/workspace", argv
    assert code == blueprint._COMPUTE_EXIT_OK, result


def test_repo_clone_records_the_commit_the_run_actually_saw(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / "seed.txt").parent.mkdir(parents=True, exist_ok=True)
    (repo / "seed.txt").write_text("x", encoding="utf-8")
    _init_repo(repo)
    monkeypatch.chdir(repo)

    _, code, result = _container_argv(
        monkeypatch, tmp_path,
        container_image="ghcr.io/acme/trainer:v3",
        command="true",
        repo_clone={"owner": "acme", "repo": "trainer", "ref": "deadbeef"},
    )
    recorded = result["environment"]["repo"]
    assert recorded["resolved_by"] == "git", recorded
    assert re.fullmatch(r"[0-9a-f]{40}", recorded["commit"] or ""), recorded
    assert recorded["dirty"] is False, recorded
    # The local path MOUNTS; it does not clone owner/repo@ref. Saying so is the
    # difference between a record and a confident lie about which ref ran.
    joined = " ".join(result["spec_warnings"])
    assert "acme/trainer" in joined, result["spec_warnings"]
    assert "deadbeef" in joined, result["spec_warnings"]
    assert code == blueprint._COMPUTE_EXIT_OK, result


def test_repo_clone_reports_a_dirty_tree(tmp_path):
    repo = tmp_path / "repo"
    (repo / "seed.txt").parent.mkdir(parents=True, exist_ok=True)
    (repo / "seed.txt").write_text("x", encoding="utf-8")
    _init_repo(repo)
    (repo / "seed.txt").write_text("changed", encoding="utf-8")
    state = blueprint._compute_git_repo_state(str(repo))
    assert state["dirty"] is True, state
    warnings = blueprint._compute_repo_clone_warnings({}, state)
    assert any("uncommitted changes" in w for w in warnings), warnings


def test_repo_state_uses_git_not_a_dot_git_isdir_probe(tmp_path):
    """In a linked worktree `.git` is a FILE, so an isdir guard skips silently."""
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    state = blueprint._compute_git_repo_state(str(not_a_repo))
    assert state["resolved_by"] == "cwd", state
    assert state["commit"] is None, state
    assert any("not a readable git repository" in w
               for w in blueprint._compute_repo_clone_warnings({}, state))


# ---------------------------------------------------------------------------
# P1-4: the realised environment is recorded, so a run is reproducible
# ---------------------------------------------------------------------------


@_needs_shell
def test_a_run_records_the_environment_it_actually_ran_in(tmp_path):
    code, result = _run_spec_json(
        tmp_path, command=WRITE_ACCURACY, declared_metric="accuracy",
    )
    assert code == blueprint._COMPUTE_EXIT_OK, result
    environment = result["environment"]
    assert environment["captured_at"], environment
    assert environment["host"]["shell"], environment
    written = Path(result["environment_file"])
    assert written.name == blueprint._COMPUTE_ENVIRONMENT_FILENAME
    assert json.loads(written.read_text(encoding="utf-8"))["host"] == environment["host"]


@_needs_shell
def test_the_package_set_is_frozen_inside_the_environment_that_ran(tmp_path):
    """`pip freeze` runs in the experiment shell, after setup_step, not beside it.

    The skip below asks the EXPERIMENT SHELL whether it has pip at all. Skipping
    on "no packages were recorded" instead would make this test pass happily on
    the very regression it exists to catch.
    """
    _, probe = _run_spec_json(
        tmp_path,
        # Reuses WRITE_ACCURACY so the probe carries no quoting of its own:
        # a metric comes back only if the experiment shell has pip.
        command=PIP_PROBE,
    )
    if not (probe.get("metrics") or {}).get("accuracy"):
        pytest.skip("no pip inside the experiment shell on this host")

    code, result = _run_spec_json(
        tmp_path, command=WRITE_ACCURACY, declared_metric="accuracy",
    )
    packages = result["environment"]["python_packages"]
    assert packages, "the shell has pip, so the run must record what it froze"
    assert any("==" in line for line in packages), packages
    assert code == blueprint._COMPUTE_EXIT_OK, result


@_needs_shell
def test_a_command_that_exits_the_shell_is_still_recorded_and_still_reports_7(tmp_path):
    """`exit 7` ends the shell on the spot. The trap must still fire, and $? survive.

    This is the ordinary shape of a failing experiment, and it is exactly the
    run whose environment you want recorded.
    """
    _, probe = _run_spec_json(tmp_path, command=PIP_PROBE)
    code, result = _run_spec_json(tmp_path, command="exit 7")
    assert result["exit_code"] == 7, result
    assert code == blueprint._COMPUTE_EXIT_COMMAND_FAILED, result
    if (probe.get("metrics") or {}).get("accuracy"):
        assert result["environment"]["python_packages"], (
            "the capture trap did not fire for a command that exited the shell"
        )


def test_the_capture_is_an_exit_trap_installed_before_the_command():
    """An appended epilogue would never run for a command that exits the shell."""
    program = blueprint._compute_shell_command(
        blueprint._ShellPlan("bash", blueprint._COMPUTE_SHELL_POSIX, "bash"),
        "python3 train.py",
        {"BLUEPRINT_RESULT_FILE": "/tmp/r.json"},
    )
    head, sep, tail = program.partition("python3 train.py")
    assert sep, program
    assert "trap __bp_capture_environment EXIT" in head, head
    assert tail.strip() == "", "nothing may run after the experiment command"
    # $? saved first and returned last, so recording cannot rewrite the verdict.
    handler = blueprint._COMPUTE_ENVIRONMENT_CAPTURE
    assert handler.index("__bp_status=$?") < handler.index("pip freeze")
    assert handler.index("pip freeze") < handler.index("return $__bp_status")
    # An opt-out exists only for callers that build a program by hand.
    bare = blueprint._compute_shell_command(
        blueprint._ShellPlan("bash", blueprint._COMPUTE_SHELL_POSIX, "bash"),
        "python3 train.py", {}, capture_environment=False,
    )
    assert "__bp_capture_environment" not in bare
    assert bare.endswith("python3 train.py")


def test_a_mutable_tag_still_records_the_digest_that_actually_ran(monkeypatch):
    """`container_image_digest_pinned` is a claim about the REFERENCE and fails open."""
    block, _ = _resolve_with(monkeypatch, local=_LOCAL_IDENTITY, image="ghcr.io/acme/trainer:latest")
    assert block["digest_pinned_reference"] is False
    assert block["digest"] == "ghcr.io/acme/trainer@sha256:" + "b" * 64
    assert block["reproduce_reference"] == block["digest"]


def test_an_image_built_here_and_pushed_nowhere_still_pins_what_ran(monkeypatch):
    block, _ = _resolve_with(
        monkeypatch, local={"id": "sha256:" + "d" * 64, "repo_digests": []},
        image="blueprint-selftest:local",
    )
    assert block["digest"] == "sha256:" + "d" * 64
    assert block["reproduce_reference"] == "sha256:" + "d" * 64


@_needs_shell
def test_env_override_values_are_never_written_into_the_record(tmp_path):
    """Overrides routinely carry tokens; the record keeps names, never values."""
    secret = "tok-" + "9" * 20
    code, result = _run_spec_json(
        tmp_path, command=WRITE_ACCURACY, declared_metric="accuracy",
        env={"HF_TOKEN": secret},
    )
    assert code == blueprint._COMPUTE_EXIT_OK, result
    assert result["environment"]["env_override_keys"] == ["HF_TOKEN"]
    assert secret not in json.dumps(result)
    assert secret not in Path(result["environment_file"]).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# P1-5: the contract is documented for a HUMAN, not only in agent prompts
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_COMPUTE_DOC = _REPO_ROOT / "docs" / "COMPUTE_RUN_LOCAL.md"


def test_compute_run_is_documented_for_humans():
    assert _COMPUTE_DOC.is_file(), f"{_COMPUTE_DOC} is missing"
    text = _COMPUTE_DOC.read_text(encoding="utf-8")
    for token in (
        "blueprint compute run",
        "BLUEPRINT_RESULT_FILE",
        "BLUEPRINT_ARTIFACT_DIR",
        "BLUEPRINT_LOCAL_GPU",
        "repo_clone",
        "setup_step",
        "--pull",
        "arms",
        blueprint._COMPUTE_ENVIRONMENT_FILENAME,
    ):
        assert token in text, f"docs/COMPUTE_RUN_LOCAL.md never mentions {token!r}"


def test_the_documented_exit_codes_match_the_shipped_constants():
    """A doc that drifts from the contract is worse than no doc."""
    text = _COMPUTE_DOC.read_text(encoding="utf-8")
    for name, value in (
        ("ran, measured", blueprint._COMPUTE_EXIT_OK),
        ("command itself exited non-zero", blueprint._COMPUTE_EXIT_COMMAND_FAILED),
        ("nothing was measured", blueprint._COMPUTE_EXIT_HARNESS_FAILED),
        ("NOT MET", blueprint._COMPUTE_EXIT_CRITERION_NOT_MET),
    ):
        row = re.search(rf"^\|\s*`?(\d+)`?\s*\|[^|]*{re.escape(name)}", text,
                        re.IGNORECASE | re.MULTILINE)
        assert row, f"no exit-code table row documents {name!r}"
        assert int(row.group(1)) == value, (name, row.group(1), value)


def test_the_docs_index_links_the_compute_run_guide():
    index = (_REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    assert "COMPUTE_RUN_LOCAL.md" in index


# ---------------------------------------------------------------------------
# REAL container runs.
#
# Everything above this line that touches docker is MOCKED. The tests below are
# not: they build an image on this machine, run a real experiment inside it,
# and assert on the metric that came back. That distinction is the whole point
# -- the repo's pre-existing container coverage was entirely `subprocess.run`-
# mocked argv assertions, which is exactly why a container path that could not
# run a local image at all went unnoticed.
#
# They skip where no docker daemon or no local base image is available (CI's
# standalone lane, and the sharded lane inside the test image).
# ---------------------------------------------------------------------------

_DOCKER_CLI = shutil.which("docker")


_BASE_IMAGE_CANDIDATES = ("python:3.11-slim", "python:3-slim", "python:3.12-slim")


@functools.lru_cache(maxsize=1)
def _probe_base_image() -> "str | None":
    """Pick a python base image that is ALREADY on this host.

    ONE docker call, memoised, and reached only from the `base_image` fixture
    below -- never at import. `docker image inspect` reports every reference it
    found and exits non-zero for the rest, so the tags are read out of stdout
    rather than probed one at a time.
    """
    if not _DOCKER_CLI:
        return None
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", *_BASE_IMAGE_CANDIDATES,
             "--format", "{{index .RepoTags 0}}"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:  # noqa: BLE001 -- no daemon means "skip", not "fail"
        return None
    found = {ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()}
    for candidate in _BASE_IMAGE_CANDIDATES:
        if candidate in found:
            return candidate
    return None


@pytest.fixture(scope="session")
def base_image() -> str:
    """The locally present python slim base image, or skip the test.

    Probed HERE rather than at module scope. A module-level probe runs during
    COLLECTION, before any test executes, so a present-but-unresponsive docker
    socket burns the full subprocess timeout out of this file's per-file budget
    in backend/testing/test_scripts_test_lane.py without a single test having
    run. Deferring it into a session fixture means only the docker tests --
    which are skipped outright when there is no daemon -- ever pay for it.
    """
    image = _probe_base_image()
    if image is None:
        pytest.skip(
            "needs a docker daemon and a locally present python slim base image"
        )
    return image


@pytest.mark.timeout(300)
def test_real_locally_built_image_runs_an_experiment_end_to_end(tmp_path, base_image):
    """The end-to-end proof for P1-1, with a tag that exists in NO registry.

    Before the fix this exact spec produced:
        error: docker pull '<tag>' failed (exit 1)   EXITCODE=2
    """
    tag = f"blueprint-compute-selftest:{uuid.uuid4().hex[:12]}"
    dockerfile = f"FROM {base_image}\nRUN printf '0.9312' > /opt/bp_baseline\n"
    build = subprocess.run(
        ["docker", "build", "-t", tag, "-"],
        input=dockerfile.encode(), capture_output=True, timeout=180,
    )
    assert build.returncode == 0, build.stderr.decode("utf-8", "replace")[-2000:]
    try:
        # The tag is registry-less: a pull CANNOT succeed. This is what made the
        # old unconditional-pull path fatal for every locally built image.
        pull = subprocess.run(
            ["docker", "pull", tag], capture_output=True, timeout=120,
        )
        assert pull.returncode != 0, "the fixture tag must exist in no registry"

        code, result = _run_spec_json(
            tmp_path,
            container_image=tag,
            setup_step="printf '0.0088' > /tmp/bp_delta",
            command=(
                "python3 -c \"import os,json;"
                "b=float(open('/opt/bp_baseline').read());"
                "d=float(open('/tmp/bp_delta').read());"
                "open(os.environ['BLUEPRINT_RESULT_FILE'],'w')"
                ".write(json.dumps({'accuracy': round(b+d,4)}))\""
            ),
            declared_metric="accuracy",
            success_criterion={"metric": "accuracy", "operator": ">=", "threshold": 0.93},
        )
        assert code == blueprint._COMPUTE_EXIT_OK, result
        # 0.9312 comes from the IMAGE, 0.0088 from the SETUP STEP. Getting 0.94
        # back proves both halves reached the container.
        assert result["metrics"]["accuracy"] == 0.94, result
        assert result["verdict"]["verdict"] == "MET", result
        image_block = result["environment"]["container_image"]
        assert image_block["source"] == "local", image_block
        assert "pull_failed_exit_code" not in image_block, image_block
        assert image_block["digest"], image_block
        # The freeze came from inside the container, not from the host.
        assert result["environment"]["python_packages"], result["environment"]
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, timeout=120)


@pytest.mark.timeout(300)
def test_real_repo_clone_puts_this_checkout_inside_the_container(tmp_path, base_image):
    """P1-3 for real: repo_clone is the only way to get code into the container."""
    repo = tmp_path / "repo"
    (repo / "bp_selftest_pkg").mkdir(parents=True)
    (repo / "bp_selftest_pkg" / "__init__.py").write_text(
        "SCORE = 0.771\n", encoding="utf-8"
    )
    _init_repo(repo)

    tag = f"blueprint-compute-selftest:{uuid.uuid4().hex[:12]}"
    build = subprocess.run(
        ["docker", "build", "-t", tag, "-"],
        input=f"FROM {base_image}\n".encode(), capture_output=True, timeout=180,
    )
    assert build.returncode == 0, build.stderr.decode("utf-8", "replace")[-2000:]
    cwd = os.getcwd()
    try:
        # Run from a SUBDIRECTORY: the mount must still be the repository root.
        os.chdir(repo / "bp_selftest_pkg")
        code, result = _run_spec_json(
            tmp_path,
            container_image=tag,
            command=(
                "python3 -c \"import os,json,bp_selftest_pkg as p;"
                "open(os.environ['BLUEPRINT_RESULT_FILE'],'w')"
                ".write(json.dumps({'score': p.SCORE}))\""
            ),
            declared_metric="score",
            repo_clone={"owner": "acme", "repo": "selftest", "ref": "main"},
        )
        assert code == blueprint._COMPUTE_EXIT_OK, result
        assert result["metrics"]["score"] == 0.771, result
        assert result["environment"]["repo"]["commit"], result["environment"]["repo"]
    finally:
        os.chdir(cwd)
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, timeout=120)


@pytest.mark.timeout(300)
def test_real_missing_image_with_pull_never_fails_loudly(tmp_path, base_image):
    """No usable image is still fatal -- the fix narrows the failure, not removes it."""
    args = blueprint.build_parser().parse_args(
        ["compute", "run", "--pull", "never", "--local",
         _spec(tmp_path, container_image=f"blueprint-absent:{uuid.uuid4().hex[:12]}",
               command="true", declared_metric="accuracy")]
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit) as exc:
        blueprint._compute_run_local(args)
    assert exc.value.code == blueprint._COMPUTE_EXIT_HARNESS_FAILED
