"""How `blueprint compute list` / `compute show` RENDER, with no network.

The backend already derives the operator-facing truth on every compute-job
payload -- criterion_status, experiment_status, outcome (see
app/compute_outcome_status.py, merged in by _build_compute_job_payload) -- and
the CLI used to throw all three away and re-render `criterion_verdict.met`, a
bool. That bool folds "the threshold was measured and missed" together with
"the metric was never measured at all", so an UNMEASURED run read as a clean
scientific failure.

Every test here calls _render_compute_list / _render_compute_show directly, so
a payload can carry exactly the fields under test and nothing else. That is
also why they live in their own file rather than in test_blueprint_cli.py: the
shared mock_api list fixture there deliberately still serves the pre-upgrade
shape (no derived fields), which is what test_compute_list_human_render pins
as the backward-compatible fallback. The two tests that DO drive
blueprint.main() for this change stay next to that fixture.
"""
import json

import blueprint
import blueprint_api
import pytest


@pytest.fixture(autouse=True)
def _hermetic_blueprint_config(monkeypatch, tmp_path):
    """Resolve load_config() without touching the host.

    The two tests below drive blueprint.main(), and several helpers on the way
    to the mocked api() call the real load_config() unconditionally. The CI
    lane runs this file with --noconftest in a container with no config
    anywhere in blueprint_api._config_search_paths, so that call raises there
    while passing on a dev host with an ambient config. See the longer note on
    the same fixture in test_blueprint_cli.py.
    """
    dummy_config = tmp_path / "hermetic-blueprint-config.json"
    dummy_config.write_text(
        json.dumps({"api_url": "https://blueprint-cli-test.invalid", "api_key": "test-key"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BLUEPRINT_CONFIG", str(dummy_config))
    monkeypatch.setattr(blueprint_api, "_config_cache", None)


def _render_list(jobs, **extra):
    payload = {"compute_jobs": jobs}
    payload.update(extra)
    blueprint._render_compute_list(payload)


def test_compute_list_prefers_derived_criterion_status_over_the_met_bool(capsys):
    """An UNMEASURED run must not render as a measured miss.

    `met=False` on a job whose metric was never observed is the specific lie
    this replaces: cjob-eee0 in the mock fixture is SUCCEEDED with
    `measurable: False`, and the operator reading `met=False` concludes the
    hypothesis was tested and refuted when nothing was tested at all.
    """
    _render_list([
        {
            "id": "cjob-eee0-0000-0000-000000000005",
            "status": "SUCCEEDED",
            "task_title": "Failed arms test",
            "task_type": "compute_experiment",
            "criterion_status": "UNMEASURED",
            "experiment_status": "CRITERION_UNMEASURED",
            "criterion_verdict": {
                "met": False, "metric": "latency", "operator": "lt",
                "threshold": 100, "value": None, "measurable": False,
            },
        },
    ])
    out = capsys.readouterr().out
    assert "crit=UNMEASURED" in out
    assert "met=False" not in out, (
        "the raw bool must not be shown once the backend serves the derived "
        f"status; got: {out!r}"
    )


def test_compute_list_distinguishes_a_measured_miss_from_an_unmeasured_run(capsys):
    """The two cases the `met` bool collapsed render differently."""
    _render_list([
        {"id": "cjob-1", "status": "SUCCEEDED", "task_title": "measured miss",
         "task_type": "compute_experiment", "criterion_status": "NOT_MET",
         "criterion_verdict": {"met": False, "metric": "acc", "value": 0.4,
                               "measurable": True}},
        {"id": "cjob-2", "status": "SUCCEEDED", "task_title": "never measured",
         "task_type": "compute_experiment", "criterion_status": "UNMEASURED",
         "criterion_verdict": {"met": False, "metric": "acc", "value": None,
                               "measurable": False}},
    ])
    out = capsys.readouterr().out
    assert "crit=NOT_MET (acc=0.4)" in out
    assert "crit=UNMEASURED" in out


def test_compute_list_omits_the_criterion_cell_when_none_was_declared(capsys):
    """NOT_DECLARED is not a verdict; rendering it as one is noise."""
    _render_list([
        {"id": "cjob-1", "status": "RUNNING", "task_title": "no criterion",
         "task_type": "compute_experiment", "criterion_status": "NOT_DECLARED"},
    ])
    out = capsys.readouterr().out
    assert "crit=" not in out
    assert "NOT_DECLARED" not in out


def test_compute_list_falls_back_to_the_met_bool_for_a_pre_upgrade_backend(capsys):
    """A backend that does not serve criterion_status still renders a verdict."""
    _render_list([
        {"id": "cjob-1", "status": "SUCCEEDED", "task_title": "old payload",
         "task_type": "compute_experiment",
         "criterion_verdict": {"met": True, "metric": "latency_ms",
                               "value": 80.0, "measurable": True}},
    ])
    out = capsys.readouterr().out
    assert "met=True (latency_ms=80.0)" in out


def test_compute_list_flags_a_job_the_watchdog_is_cutting(capsys):
    """`watchdog_state == 'cutting'` means the next poll pass kills the job.

    An operator scanning the list should not have to open a job to learn it is
    about to be terminated, nor guess why.
    """
    _render_list([
        {"id": "cjob-1", "status": "RUNNING", "task_title": "stalled trainer",
         "task_type": "compute_experiment", "watchdog_state": "cutting",
         "watchdog_cut_reason": "plateau"},
        {"id": "cjob-2", "status": "RUNNING", "task_title": "healthy trainer",
         "task_type": "compute_experiment", "watchdog_state": "protected"},
    ])
    lines = [ln for ln in capsys.readouterr().out.split("\n") if "trainer" in ln]
    stalled = next(ln for ln in lines if "stalled" in ln)
    healthy = next(ln for ln in lines if "healthy" in ln)
    assert "watchdog cutting (plateau)" in stalled
    assert "watchdog" not in healthy, "a protected job needs no marker"


def test_compute_list_does_not_flag_a_terminal_job_as_being_cut(capsys):
    """A finished job's last watchdog reading is history, not a live threat."""
    _render_list([
        {"id": "cjob-1", "status": "FAILED", "task_title": "already dead",
         "task_type": "compute_experiment", "watchdog_state": "cutting",
         "watchdog_cut_reason": "plateau"},
    ])
    assert "watchdog cutting" not in capsys.readouterr().out


def test_compute_list_shows_why_a_terminal_job_died(capsys):
    """Surface reasons, not states: FAILED alone does not tell the operator
    whether to requeue, fix the code, or raise the spend cap."""
    _render_list([
        {"id": "cjob-1", "status": "FAILED", "task_title": "oom trainer",
         "task_type": "compute_experiment",
         "terminal_reason": "container exited 137 (out of memory)"},
    ])
    out = capsys.readouterr().out
    assert "reason=container exited 137 (out of memory)" in out


def test_compute_list_empty_names_the_window_that_produced_it(capsys):
    """`(none)` is a claim about a WINDOW, not about the project.

    `compute list` defaults to active jobs plus terminal ones finished in the
    last 6h. Printing a bare `(none)` told an operator the project had no
    compute at all, when a run that ended seven hours ago was simply outside
    the default window -- and gave no hint that a wider one was askable.
    """
    _render_list([], window={"all_terminal": False,
                             "include_finished_minutes": 360, "limit": 20})
    out = capsys.readouterr().out
    assert "(none)" in out
    assert "last 6h" in out
    assert "--include-finished" in out
    assert "--all-terminal" in out


def test_compute_list_empty_reports_a_widened_window_honestly(capsys):
    """The disclosure must name the window actually used, not the default."""
    _render_list([], window={"all_terminal": False,
                             "include_finished_minutes": 4320, "limit": 20})
    out = capsys.readouterr().out
    assert "last 3d" in out

    _render_list([], window={"all_terminal": True,
                             "include_finished_minutes": None, "limit": 20})
    out = capsys.readouterr().out
    assert "searched every terminal job" in out
    assert "--all-terminal" not in out, (
        "do not suggest widening a search that was already unbounded"
    )


def test_compute_list_empty_names_the_status_filter_too(capsys):
    """A filter is the other reason a non-empty project renders empty."""
    _render_list([], window={"all_terminal": True}, status_filter="FAILED")
    out = capsys.readouterr().out
    assert "filtered to status=FAILED" in out


def test_compute_show_renders_the_fused_experiment_verdict(capsys):
    """`status: SUCCEEDED` alone answers "did the process exit 0?", not "did the
    experiment work?". The backend fuses the two into experiment_status."""
    blueprint._render_compute_show({"compute_job": {
        "id": "cjob-eee0-0000-0000-000000000005",
        "status": "SUCCEEDED",
        "execution_status": "SUCCEEDED",
        "criterion_status": "UNMEASURED",
        "experiment_status": "CRITERION_UNMEASURED",
        "outcome": "success",
        "task_title": "Failed arms test",
        "task_type": "compute_experiment",
        "final_cost_usd": 2.1,
    }})
    out = capsys.readouterr().out
    assert "status    : SUCCEEDED" in out
    assert "result    : CRITERION_UNMEASURED" in out
    assert "criterion : UNMEASURED" in out
    assert "outcome   : success" in out


def test_compute_show_omits_a_redundant_or_absent_fused_verdict(capsys):
    """An active job's experiment_status just echoes its status, and a
    pre-upgrade payload has none; neither should print a blank line."""
    blueprint._render_compute_show({"compute_job": {
        "id": "cjob-1", "status": "RUNNING", "experiment_status": "RUNNING",
        "criterion_status": "NOT_DECLARED", "task_title": "t",
        "task_type": "compute_experiment",
    }})
    out = capsys.readouterr().out
    assert "result    :" not in out
    assert "criterion :" not in out

    blueprint._render_compute_show({"compute_job": {
        "id": "cjob-2", "status": "SUCCEEDED", "task_title": "t",
        "task_type": "compute_experiment",
    }})
    out = capsys.readouterr().out
    assert "result    :" not in out
    assert "outcome   :" not in out


def test_compute_show_reports_a_watchdog_about_to_cut(capsys):
    """The same 'cutting' truth the list marks, with the reason spelled out."""
    blueprint._render_compute_show({"compute_job": {
        "id": "cjob-1", "status": "RUNNING", "task_title": "stalled",
        "task_type": "compute_experiment",
        "watchdog_state": "cutting", "watchdog_cut_reason": "plateau",
        "no_progress_timeout_seconds": 1800,
    }})
    out = capsys.readouterr().out
    assert "CUTTING NOW" in out
    assert "plateau" in out
    assert "1800s no-progress" in out


def test_compute_show_reports_a_protected_and_an_inert_watchdog(capsys):
    """'protected' and 'inert' are different answers and must read differently.

    An inert watchdog means nothing will stop a runaway job on no-progress
    grounds -- that is a fact about the job's contract the operator has to be
    able to see, not a synonym for 'fine'.
    """
    blueprint._render_compute_show({"compute_job": {
        "id": "cjob-1", "status": "RUNNING", "task_title": "healthy",
        "task_type": "compute_experiment",
        "watchdog_state": "protected", "no_progress_timeout_seconds": 1800,
        "last_progress_at": "2026-01-02T12:00:00",
    }})
    out = capsys.readouterr().out
    assert "watchdog  : protected" in out
    assert "last progress 2026-01-02T12:00:00" in out
    assert "CUTTING" not in out

    blueprint._render_compute_show({"compute_job": {
        "id": "cjob-2", "status": "RUNNING", "task_title": "unwatched",
        "task_type": "compute_experiment",
        "watchdog_state": "inert", "watchdog_inert_reason": "no_declared_metric",
    }})
    out = capsys.readouterr().out
    assert "watchdog  : inert — no_declared_metric" in out


def test_compute_show_omits_the_watchdog_for_a_terminal_job(capsys):
    """A finished job has no live watchdog to report on."""
    blueprint._render_compute_show({"compute_job": {
        "id": "cjob-1", "status": "FAILED", "task_title": "dead",
        "task_type": "compute_experiment", "watchdog_state": "cutting",
        "watchdog_cut_reason": "plateau",
        "terminal_reason": "no_progress",
    }})
    out = capsys.readouterr().out
    assert "watchdog" not in out
    assert "terminal  : no_progress" in out


# ── and the same truth through blueprint.main(), not just the renderer ───────


@pytest.fixture
def mock_api(monkeypatch):
    """Serve one compute-jobs page carrying the derived fields."""
    payload = {
        "compute_jobs": [
            {"id": "cjob-1", "status": "SUCCEEDED", "task_title": "measured",
             "task_type": "compute_experiment", "criterion_status": "MET",
             "experiment_status": "CRITERION_MET"},
            {"id": "cjob-2", "status": "SUCCEEDED", "task_title": "unmeasured run",
             "task_type": "compute_experiment", "criterion_status": "UNMEASURED",
             "experiment_status": "CRITERION_UNMEASURED"},
        ],
        "has_more": False,
        "summary": None,
    }
    monkeypatch.setattr(blueprint, "api", lambda *a, **k: payload)
    monkeypatch.setattr(blueprint_api, "api", lambda *a, **k: payload)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    return payload


def test_compute_list_emits_the_window_in_json(mock_api, capsys):
    """The window is machine-readable too, so a script can widen it."""
    rc = blueprint.main(["--json", "compute", "list"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["window"] == {
        "all_terminal": False,
        "include_finished_minutes": 360,
        "limit": 20,
    }


def test_compute_list_status_filter_matches_the_derived_experiment_status(
    monkeypatch, capsys
):
    """`--status CRITERION_UNMEASURED` is askable.

    Every unmeasured run is filed under execution status SUCCEEDED, so without
    this the only way to find them is to eyeball the list.
    """
    payload = {
        "compute_jobs": [
            {"id": "cjob-1", "status": "SUCCEEDED", "task_title": "measured",
             "task_type": "compute_experiment", "criterion_status": "MET",
             "experiment_status": "CRITERION_MET"},
            {"id": "cjob-2", "status": "SUCCEEDED", "task_title": "unmeasured",
             "task_type": "compute_experiment", "criterion_status": "UNMEASURED",
             "experiment_status": "CRITERION_UNMEASURED"},
        ],
        "has_more": False,
        "summary": None,
    }
    monkeypatch.setattr(blueprint, "api", lambda *a, **k: payload)
    monkeypatch.setattr(blueprint_api, "api", lambda *a, **k: payload)
    monkeypatch.setattr(blueprint, "get_project_id", lambda x=None: "proj-1")
    rc = blueprint.main(["compute", "list", "--status", "criterion_unmeasured"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "unmeasured" in out
    assert "cjob-1" not in out, "the MET job must be filtered out"
