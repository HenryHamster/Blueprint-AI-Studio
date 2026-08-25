"""Contract tests for the shared multi-lane verification receipt writer."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import verification_receipt as receipt  # noqa: E402
from testing.fixgate_attribution import Attribution  # noqa: E402

_LEGACY_PYTEST_KEYS = {
    "command",
    "harness",
    "selectors",
    "targeted",
    "use_test_postgres",
    "postgres_sidecar",
    "state",
    "current_exit_code",
    "tests",
    "skipped",
    "failures",
    "new_failures",
    "suppressed_failures",
    "disappeared_failures",
    "skipped_failures",
    "exit_code",
}


def _junit(path: Path) -> Path:
    path.write_text(
        '<testsuite tests="2">'
        '<testcase classname="testing.test_a" name="ok"/>'
        '<testcase classname="testing.test_a" name="bad"><failure>boom</failure></testcase>'
        "</testsuite>",
        encoding="utf-8",
    )
    return path


def _backend_attribution() -> str:
    return json.dumps(
        {
            "state": "preexisting_test_failure",
            "blocking": False,
            "current_exit_code": 1,
            "new_failures": {},
            "suppressed_failures": {
                "testing/test_a.py::bad::failure": {
                    "detail": "boom",
                    "reason": "same route/test/failure kind failed on baseline",
                }
            },
            "disappeared_failures": {},
            "skipped_failures": {},
        }
    )


def test_lane_identities_never_collide_across_producers() -> None:
    """Each producer owns a distinct, suite-qualified receipt identity."""

    identities = {
        receipt.LANE_BACKEND_PYTEST,
        receipt.LANE_STATIC_CORPUS,
        receipt.vitest_lane("admin-ui"),
        receipt.vitest_lane("agent"),
        receipt.vitest_lane("admin-ui-viewport"),
        receipt.typecheck_lane("admin-ui"),
        receipt.typecheck_lane("agent"),
    }
    assert identities == {
        "backend-pytest",
        "static-corpus",
        "vitest:admin-ui",
        "vitest:agent",
        "vitest:admin-ui-viewport",
        "typecheck:admin-ui",
        "typecheck:agent",
    }
    assert receipt.vitest_lane("agent") != receipt.typecheck_lane("agent")


def test_lanes_accumulate_instead_of_overwriting(tmp_path: Path) -> None:
    """Three lanes writing in sequence leave three distinct blocks."""

    path = tmp_path / "receipt.json"
    assert receipt.record_lane(
        receipt.vitest_lane("agent"),
        state="preexisting_test_failure",
        suppressed_failures={"src/a.test.ts::x::failure": "inherited"},
        receipt_path=path,
    )
    assert receipt.record_lane(
        receipt.typecheck_lane("admin-ui"),
        state="preexisting_type_failure",
        suppressed_failures={"src/a.tsx::TS2345: bad::failure": "inherited type"},
        receipt_path=path,
    )
    assert receipt.record_lane(
        receipt.LANE_STATIC_CORPUS,
        state="preexisting_test_failure",
        suppressed_failures={"testing/test_b.py::y::failure": "inherited corpus"},
        receipt_path=path,
    )

    lanes = json.loads(path.read_text(encoding="utf-8"))["lanes"]
    assert set(lanes) == {"vitest:agent", "typecheck:admin-ui", "static-corpus"}
    assert lanes["typecheck:admin-ui"]["suppressed_failures"] == {
        "src/a.tsx::TS2345: bad::failure": {"detail": "inherited type"}
    }
    assert lanes["static-corpus"]["state"] == "preexisting_test_failure"


def test_rerunning_a_lane_replaces_rather_than_duplicates_its_block(
    tmp_path: Path,
) -> None:
    """Idempotency: the second run of a lane is the only one that survives."""

    path = tmp_path / "receipt.json"
    receipt.record_lane(
        receipt.vitest_lane("agent"),
        state="test_failure",
        blocking=True,
        new_failures={"src/a.test.ts::x::failure": "first"},
        receipt_path=path,
    )
    receipt.record_lane(
        receipt.vitest_lane("agent"),
        state="passed",
        receipt_path=path,
    )

    lanes = json.loads(path.read_text(encoding="utf-8"))["lanes"]
    assert list(lanes) == ["vitest:agent"]
    assert lanes["vitest:agent"]["state"] == "passed"
    assert lanes["vitest:agent"]["new_failures"] == {}
    assert lanes["vitest:agent"]["blocking"] is False


def test_backend_lane_does_not_clobber_an_earlier_lane(tmp_path: Path) -> None:
    """Lane order is irrelevant: the backend lane merges, it does not rewrite."""

    path = tmp_path / "receipt.json"
    receipt.record_lane(
        receipt.vitest_lane("admin-ui"),
        state="preexisting_test_failure",
        suppressed_failures={"src/a.test.tsx::x::failure": "inherited"},
        receipt_path=path,
    )
    receipt.record_backend_pytest(
        receipt=path,
        task_id="task-1",
        directory="backend",
        harness="scripts/test-backend.sh",
        selectors_json='["testing/test_a.py"]',
        attribution_json=_backend_attribution(),
        junit=_junit(tmp_path / "current.xml"),
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload["lanes"]) == {"vitest:admin-ui", "backend-pytest"}
    assert payload["lanes"]["vitest:admin-ui"]["suppressed_failures"] == {
        "src/a.test.tsx::x::failure": {"detail": "inherited"}
    }
    # BACK-COMPAT: pr-helper.ts reads this exact top-level block.
    assert set(payload["pytest"]) == _LEGACY_PYTEST_KEYS
    assert payload["pytest"]["state"] == "preexisting_test_failure"
    assert payload["pytest"]["tests"] == 2
    assert payload["pytest"]["failures"] == 1
    assert payload["pytest"]["selectors"] == ["testing/test_a.py"]
    assert payload["task_id"] == "task-1"
    assert payload["ruff"]["exit_code"] == 0


def test_blocking_backend_lane_keeps_diagnosis_but_not_a_pass_credential(
    tmp_path: Path,
) -> None:
    """A blocked run survives with all attribution and a nonzero receipt code."""

    path = tmp_path / "receipt.json"
    attribution = json.dumps(
        {
            "state": "coverage_regression",
            "blocking": True,
            "current_exit_code": 0,
            "error": "route coverage regressed",
            "new_failures": {"test_a::coverage": "route absent"},
            "suppressed_failures": {"test_b::failure": "inherited"},
            "disappeared_failures": {"test_c::failure": "disappeared"},
            "skipped_failures": {"test_d::failure": "skipped"},
        }
    )

    receipt.record_backend_pytest(
        receipt=path,
        task_id="blocked-task",
        directory="backend",
        harness="scripts/test-backend.sh",
        selectors_json='["testing/test_a.py"]',
        attribution_json=attribution,
        junit=tmp_path / "missing-current.xml",
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    lane = payload["lanes"]["backend-pytest"]
    assert lane["state"] == "coverage_regression"
    assert lane["blocking"] is True
    assert lane["current_exit_code"] == 0
    assert lane["error"] == "route coverage regressed"
    assert set(lane) >= {
        "new_failures",
        "suppressed_failures",
        "disappeared_failures",
        "skipped_failures",
    }
    assert payload["pytest"]["current_exit_code"] == 0
    assert payload["pytest"]["exit_code"] == 1
    assert payload["pytest"]["new_failures"] == {
        "test_a::coverage": "route absent"
    }


def test_a_later_lane_does_not_clobber_the_backend_block(tmp_path: Path) -> None:
    """The reverse order preserves the legacy consumer's block untouched."""

    path = tmp_path / "receipt.json"
    receipt.record_backend_pytest(
        receipt=path,
        task_id="task-1",
        directory="backend",
        harness="scripts/test-backend.sh",
        selectors_json='["testing/test_a.py"]',
        attribution_json=_backend_attribution(),
        junit=_junit(tmp_path / "current.xml"),
    )
    before = json.loads(path.read_text(encoding="utf-8"))["pytest"]
    receipt.record_lane(
        receipt.typecheck_lane("agent"), state="passed", receipt_path=path
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["pytest"] == before
    assert set(payload["lanes"]) == {"backend-pytest", "typecheck:agent"}


def test_a_malformed_receipt_does_not_fail_the_writing_lane(tmp_path: Path) -> None:
    """An unusable prior sidecar is replaced, never raised on."""

    path = tmp_path / "receipt.json"
    path.write_text("{not json at all", encoding="utf-8")
    assert receipt.record_lane(
        receipt.vitest_lane("agent"), state="passed", receipt_path=path
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["lanes"]["vitest:agent"]["state"] == "passed"


def test_an_unwritable_receipt_warns_without_raising(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A receipt failure is advisory: it returns False instead of exploding."""

    unwritable = tmp_path / "missing.txt" / "receipt.json"
    (tmp_path / "missing.txt").write_text("not a directory", encoding="utf-8")
    assert (
        receipt.record_lane(
            receipt.vitest_lane("agent"), state="passed", receipt_path=unwritable
        )
        is False
    )
    assert "could not record lane vitest:agent" in capsys.readouterr().err


def test_a_missing_task_identity_skips_writing_rather_than_failing() -> None:
    """No identity means no receipt — never a new fail-closed gate."""

    assert receipt.resolve_receipt_path({}) is None
    assert receipt.resolve_receipt_path({"TASK_ID": "   "}) is None
    assert receipt.resolve_receipt_path({"BLUEPRINT_VERIFICATION_RECEIPT": ""}) is None
    assert receipt.record_lane(
        receipt.vitest_lane("agent"), state="passed", env={}
    ) is False


def test_the_receipt_path_follows_the_gate_before_the_task_id() -> None:
    """./check exports the resolved path; TASK_ID is only the fallback."""

    assert receipt.resolve_receipt_path({"TASK_ID": "abc"}) == Path(
        "/tmp/project_verification_receipt_abc.json"
    )
    assert receipt.resolve_receipt_path(
        {"TASK_ID": "abc", "BLUEPRINT_VERIFICATION_RECEIPT": "/tmp/elsewhere.json"}
    ) == Path("/tmp/elsewhere.json")


def test_record_attribution_stores_all_four_structured_maps(tmp_path: Path) -> None:
    """Structured identities from the shared comparator, not rendered logs."""

    path = tmp_path / "receipt.json"
    receipt.record_attribution(
        receipt.LANE_STATIC_CORPUS,
        Attribution(
            new_failures={"testing/test_a.py::new::failure": "new detail"},
            suppressed_failures={"testing/test_a.py::old::failure": "old detail"},
            disappeared_failures={"testing/test_b.py::gone::failure": "gone detail"},
            skipped_failures={"testing/test_c.py::skip::failure": "skip detail"},
        ),
        state="test_failure",
        blocking=True,
        receipt_path=path,
    )

    block = json.loads(path.read_text(encoding="utf-8"))["lanes"]["static-corpus"]
    assert block["blocking"] is True
    for key, identity in (
        ("new_failures", "testing/test_a.py::new::failure"),
        ("suppressed_failures", "testing/test_a.py::old::failure"),
        ("disappeared_failures", "testing/test_b.py::gone::failure"),
        ("skipped_failures", "testing/test_c.py::skip::failure"),
    ):
        assert list(block[key]) == [identity]


def test_failure_maps_are_bounded_for_the_durable_receipt(tmp_path: Path) -> None:
    """Identity count and detail length stay inside the publication bounds."""

    path = tmp_path / "receipt.json"
    receipt.record_lane(
        receipt.vitest_lane("agent"),
        state="test_failure",
        new_failures={f"src/a.test.ts::case{index}::failure": "x" * 9000 for index in range(250)},
        receipt_path=path,
    )
    block = json.loads(path.read_text(encoding="utf-8"))["lanes"]["vitest:agent"]
    assert len(block["new_failures"]) == receipt.MAX_IDENTITIES_PER_MAP
    assert all(
        len(entry["detail"]) <= receipt.MAX_DETAIL_CHARS
        for entry in block["new_failures"].values()
    )


def test_the_merge_write_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed write leaves the previous receipt intact, not a truncated file."""

    path = tmp_path / "receipt.json"
    receipt.record_lane(receipt.vitest_lane("agent"), state="passed", receipt_path=path)
    intact = path.read_text(encoding="utf-8")

    def exploding_replace(*_: object, **__: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(receipt.os, "replace", exploding_replace)
    assert (
        receipt.record_lane(
            receipt.typecheck_lane("agent"), state="passed", receipt_path=path
        )
        is False
    )
    assert path.read_text(encoding="utf-8") == intact
    assert not [entry for entry in tmp_path.iterdir() if entry.name.endswith(".tmp")]


def test_an_invalid_lane_identity_is_rejected() -> None:
    """Lane identities are a controlled vocabulary, not free text."""

    for lane in ("", "Backend Pytest", "vitest:agent:extra", "../escape"):
        with pytest.raises(ValueError):
            receipt.build_lane_block(lane, state="passed")


def test_the_backend_lane_cli_is_what_the_gates_invoke(tmp_path: Path) -> None:
    """Both gate shells call the real helper rather than an inline heredoc."""

    inline_writer = "import datetime,json,sys,xml.etree.ElementTree as ET; out,task"

    # The true-developer-checkout gate lives in this repo beside the writer, so
    # it calls it unconditionally and keeps no inline copy at all.
    default_gate = (REPO_ROOT / "scripts" / "check-default.sh").read_text(encoding="utf-8")
    assert "python3 scripts/verification_receipt.py backend-pytest" in default_gate
    assert inline_writer not in default_gate

    # The GENERATED fragment is emitted for any project declaring the
    # blueprint-v1 verification protocol, including foreign workspaces that do
    # not ship this writer. It prefers the writer behind a readability guard and
    # keeps the self-contained inline writer as the fallback, so a missing
    # helper can never be the reason ./check blocks a green lane.
    fragment = (
        REPO_ROOT / "agent" / "src" / "prompts" / "check_backend_verification.sh"
    ).read_text(encoding="utf-8")
    assert "python3 scripts/verification_receipt.py backend-pytest" in fragment
    assert "if [ -r scripts/verification_receipt.py ]; then" in fragment
    assert inline_writer in fragment
    # Structural, not textual: the only invocation of the helper sits INSIDE the
    # readability guard, and the inline fallback is the guard's `elif` arm.
    guard_at = fragment.index("if [ -r scripts/verification_receipt.py ]; then")
    invocations = [
        match.start()
        for match in re.finditer(
            r"python3 scripts/verification_receipt\.py backend-pytest --receipt", fragment
        )
    ]
    fallback_at = fragment.rindex("elif python3 -c ")
    assert invocations, "the fragment must still prefer the shared writer"
    assert all(guard_at < at < fallback_at for at in invocations)
    assert 'if [ "$_verification_receipt_written" = "1" ]; then' in fragment
    assert fragment.index('if [ "$_verification_receipt_written" = "1" ]') > fallback_at

    # Both gates hand the attribution over as a FILE: one traceback per
    # suppressed baseline failure would otherwise blow the argv-size limit.
    for text in (default_gate, fragment):
        assert '--attribution-file "$_verification_attribution_file"' in text
        blocking_branch = text[text.index('if [ "$_verification_blocking" != "0" ]') :]
        assert 'FAILED+=("$_VERIFICATION_DIR verification: $_verification_state")' in blocking_branch
        assert 'rm -f "$_VERIFY_RECEIPT"' not in blocking_branch

    path = tmp_path / "receipt.json"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "verification_receipt.py"),
            "backend-pytest",
            "--receipt",
            str(path),
            "--task-id",
            "task-cli",
            "--directory",
            "backend",
            "--harness",
            "scripts/test-backend.sh",
            "--selectors-json",
            '["testing/test_a.py"]',
            "--attribution",
            _backend_attribution(),
            "--junit",
            str(_junit(tmp_path / "current.xml")),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["pytest"]["suppressed_failures"] == {
        "testing/test_a.py::bad::failure": {
            "detail": "boom",
            "reason": "same route/test/failure kind failed on baseline",
        }
    }
    assert payload["lanes"]["backend-pytest"]["suppressed_failures"] == payload[
        "pytest"
    ]["suppressed_failures"]


def test_inline_backend_abort_fallback_writes_parseable_json(tmp_path: Path) -> None:
    """The foreign-workspace abort fallback must emit valid JSON."""

    for relative in (
        Path("agent/src/prompts/check_backend_verification.sh"),
        Path("scripts/check-default.sh"),
    ):
        shell = (REPO_ROOT / relative).read_text(encoding="utf-8")
        matches = re.finditer(r"elif python3 -c '(?P<code>.*?)' \\\n", shell, re.DOTALL)
        code = next(
            match.group("code")
            for match in matches
            if "out,task,directory,state,reason,code=sys.argv[1:]" in match.group("code")
        )
        path = tmp_path / f"{relative.name}.json"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                str(path),
                "abort-task",
                "backend",
                "route_helper_unavailable",
                "route helper unavailable or invalid",
                "1",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(path.read_text(encoding="utf-8"))
        lane = payload["lanes"]["backend-pytest"]
        assert payload["version"] == 1
        assert lane["state"] == "route_helper_unavailable"
        assert lane["blocking"] is True
        assert lane["error"] == "route helper unavailable or invalid"
        assert lane["current_exit_code"] == 1


def test_every_backend_lane_abort_records_a_blocking_lane_state(
    tmp_path: Path,
) -> None:
    """Every pre-guard abort uses the shared diagnostic writer and blocks."""

    fragment = (
        REPO_ROOT / "agent" / "src" / "prompts" / "check_backend_verification.sh"
    ).read_text(encoding="utf-8")
    expected = {
        "route_helper_unavailable",
        "target_unavailable",
        "lint_failure",
        "resolver_failure",
        "selector_unavailable",
        "selector_cap_exceeded",
        "capability_receipt_failure",
        "harness_unavailable",
        "runtime_unavailable",
    }
    for state in expected:
        assert f"_record_backend_abort {state} " in fragment

        path = tmp_path / f"{state}.json"
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "verification_receipt.py"),
                "backend-pytest-diagnosis",
                "--receipt",
                str(path),
                "--task-id",
                "abort-task",
                "--directory",
                "backend",
                "--state",
                state,
                "--reason",
                f"reason for {state}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        lane = json.loads(path.read_text(encoding="utf-8"))["lanes"][
            "backend-pytest"
        ]
        assert lane["state"] == state
        assert lane["blocking"] is True
        assert lane["error"] == f"reason for {state}"
        assert lane["current_exit_code"] == 1
        assert all(lane[key] == {} for key in receipt.FAILURE_MAP_KEYS)


# ---------------------------------------------------------------------------
# Multi-lane projection into the PR body and the durable publication receipt
# ---------------------------------------------------------------------------


def _lane(name: str, state: str, suppressed: dict | None = None) -> dict:
    return {
        "lane": name,
        "state": state,
        "blocking": False,
        "new_failures": {},
        "suppressed_failures": suppressed or {},
        "disappeared_failures": {},
        "skipped_failures": {},
    }


def _multi_lane_sidecar() -> dict:
    return {
        "version": 1,
        "pytest": {
            "state": "preexisting_test_failure",
            "new_failures": {},
            "suppressed_failures": {"test_a.py::a::failure": {"detail": "backend inherited"}},
            "disappeared_failures": {},
            "skipped_failures": {},
        },
        "lanes": {
            "backend-pytest": _lane(
                "backend-pytest",
                "preexisting_test_failure",
                {"test_a.py::a::failure": {"detail": "backend inherited"}},
            ),
            "static-corpus": _lane(
                "static-corpus",
                "preexisting_test_failure",
                {"test_b.py::b::error": {"detail": "corpus inherited"}},
            ),
            "typecheck:agent": _lane("typecheck:agent", "clean"),
        },
    }


def test_render_names_the_lane_that_forgave_each_suppressed_identity():
    section = receipt.render_suppressed_section(_multi_lane_sidecar())

    assert section == (
        "### Suppressed baseline failures\n"
        "- lane `backend-pytest` (state: preexisting_test_failure)\n"
        "  - `test_a.py::a::failure`: backend inherited\n"
        "- lane `static-corpus` (state: preexisting_test_failure)\n"
        "  - `test_b.py::b::error`: corpus inherited"
    )


def test_projection_namespaces_identical_identities_from_two_lanes():
    sidecar = {"lanes": {
        "vitest:admin-ui": _lane("vitest:admin-ui", "preexisting_test_failure",
                                 {"shared::identity": {"detail": "admin-ui"}}),
        "vitest:agent": _lane("vitest:agent", "preexisting_test_failure",
                              {"shared::identity": {"detail": "agent"}}),
    }}

    projected = receipt.project_attribution(sidecar)

    assert projected["suppressed_failures"] == {
        "[vitest:agent] shared::identity": {"detail": "agent"},
        "[vitest:admin-ui] shared::identity": {"detail": "admin-ui"},
    }
    assert projected["attribution_state"] == (
        "vitest:admin-ui=preexisting_test_failure; vitest:agent=preexisting_test_failure"
    )


@pytest.mark.parametrize("wrap", [True, False])
def test_legacy_single_lane_sidecar_renders_and_projects_exactly_as_before(wrap):
    """Older and foreign stacks write no ``lanes`` key; their output is frozen."""
    attribution = {
        "state": "preexisting_test_failure",
        "new_failures": {},
        "suppressed_failures": {"b::x": "detail-b", "a::y": {"detail": "detail-a"}},
        "disappeared_failures": {},
        "skipped_failures": {},
    }
    sidecar = {"pytest": attribution} if wrap else attribution

    assert receipt.render_suppressed_section(sidecar) == (
        "### Suppressed baseline failures\n- `a::y`: detail-a\n- `b::x`: detail-b"
    )
    assert receipt.project_attribution(sidecar) == {
        "attribution_state": "preexisting_test_failure",
        "new_failures": {},
        "suppressed_failures": attribution["suppressed_failures"],
        "disappeared_failures": {},
        "skipped_failures": {},
    }


@pytest.mark.parametrize(
    "payload", [None, {}, {"lanes": "not-a-map"}, {"pytest": {"state": 7}}, {"lanes": {"x": 3}}]
)
def test_unusable_sidecars_render_nothing_and_project_nothing(payload):
    assert receipt.render_suppressed_section(payload) == ""
    assert receipt.project_attribution(payload) == {}


def test_render_command_is_a_silent_no_op_on_a_malformed_sidecar(tmp_path, capsys):
    broken = tmp_path / "receipt.json"
    broken.write_text("{not json", encoding="utf-8")

    assert receipt.render_suppressed_command(broken) == 0
    assert capsys.readouterr().out == ""


def test_project_command_leaves_the_gate_receipt_untouched_on_a_bad_sidecar(tmp_path):
    gate = tmp_path / "gate.json"
    gate.write_text(json.dumps({"status": "passed"}), encoding="utf-8")
    broken = tmp_path / "receipt.json"
    broken.write_text("{not json", encoding="utf-8")

    assert receipt.project_attribution_command(gate, broken) == 0
    assert json.loads(gate.read_text(encoding="utf-8")) == {"status": "passed"}


def test_project_command_folds_every_lane_into_the_gate_receipt(tmp_path):
    gate = tmp_path / "gate.json"
    gate.write_text(json.dumps({"status": "passed", "exit_code": 0}), encoding="utf-8")
    sidecar = tmp_path / "receipt.json"
    sidecar.write_text(json.dumps(_multi_lane_sidecar()), encoding="utf-8")

    assert receipt.main(
        ["project-attribution", "--gate", str(gate), "--receipt", str(sidecar)]
    ) == 0

    merged = json.loads(gate.read_text(encoding="utf-8"))
    assert merged["status"] == "passed"
    assert merged["attribution_state"] == (
        "backend-pytest=preexisting_test_failure; static-corpus=preexisting_test_failure; "
        "typecheck:agent=clean"
    )
    assert set(merged["suppressed_failures"]) == {
        "[backend-pytest] test_a.py::a::failure",
        "[static-corpus] test_b.py::b::error",
    }
