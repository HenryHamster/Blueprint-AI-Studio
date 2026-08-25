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
import io
import json
import os
import subprocess
import sys
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


WRITE_ACCURACY = 'printf "%s" \'{"accuracy": 0.9688}\' > "$BLUEPRINT_RESULT_FILE"'

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
def test_local_only_verb_never_touches_load_config(monkeypatch, tmp_path):
    """The exact P0-2 failure: a purely local run died on a missing API config.

    load_config() is replaced with a landmine, so any credential resolution on
    the way to the handler fails the test rather than being papered over by a
    dev host that happens to have a config file.
    """

    def _landmine(*a, **k):
        raise AssertionError("load_config() must not be reached by a local-only verb")

    monkeypatch.setattr(blueprint_api, "load_config", _landmine)
    monkeypatch.setattr(blueprint_api, "_config_cache", None)
    monkeypatch.delenv("BLUEPRINT_CONFIG", raising=False)
    monkeypatch.delenv("BLUEPRINT_API_URL", raising=False)
    monkeypatch.delenv("BLUEPRINT_API_KEY", raising=False)

    spec = _spec(tmp_path, command=WRITE_ACCURACY, declared_metric="accuracy")
    assert blueprint.main(["compute", "run", "--local", spec]) == blueprint._COMPUTE_EXIT_OK


def test_local_only_exemption_is_checked_before_the_scoped_fence(monkeypatch):
    """`compute run` is exempt from the ServiceCredential fence, by declaration."""

    def _landmine():
        raise AssertionError("credential resolved for a local-only verb")

    monkeypatch.setattr(blueprint, "_using_scoped_service_credential", _landmine)
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
