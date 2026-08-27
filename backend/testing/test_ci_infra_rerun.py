"""Unit tests for the setup-phase CI-infra rerun gate.

Covers the repo-agnostic step classifier (``_job_failed_only_at_setup``) and the
``rerun_failed_jobs`` Actions-API helper. The gate's job is to recognize a CI
failure that occurred purely during GitHub's built-in setup phase (container /
runner init) — a transient infra flake — so the orchestrator re-runs it once
instead of burning a fix cycle on a non-bug.
"""

from app import github as gh_module
from app.github_checks import (
    _GITHUB_SETUP_STEPS,
    _job_failed_only_at_setup,
    rerun_failed_jobs,
    rerun_failed_jobs_outcome,
)


def _job(*failed_steps: str) -> dict:
    return {
        "conclusion": "failure",
        "steps": [{"name": n, "conclusion": "failure"} for n in failed_steps],
    }


# ── _job_failed_only_at_setup ───────────────────────────────────────────────


def test_initialize_containers_is_setup():
    assert _job_failed_only_at_setup(_job("Initialize containers")) is True


def test_set_up_job_is_setup():
    assert _job_failed_only_at_setup(_job("Set up job")) is True


def test_user_step_failure_is_not_setup():
    assert _job_failed_only_at_setup(_job("Run backend tests")) is False


def test_mixed_setup_and_user_is_not_setup():
    # A real test failure alongside a setup hiccup must NOT be treated as infra.
    assert (
        _job_failed_only_at_setup(_job("Initialize containers", "Run backend tests"))
        is False
    )


def test_no_failed_steps_is_not_setup():
    job = {
        "conclusion": "failure",
        "steps": [{"name": "Set up job", "conclusion": "success"}],
    }
    assert _job_failed_only_at_setup(job) is False


def test_empty_or_missing_steps_is_not_setup():
    assert _job_failed_only_at_setup({"conclusion": "failure", "steps": []}) is False
    assert _job_failed_only_at_setup({"conclusion": "failure"}) is False


def test_setup_step_names_are_github_builtins():
    # Guards the repo-agnostic contract: these are GitHub's own generated step
    # names, present in every Actions job — not Blueprint workflow config.
    assert "Initialize containers" in _GITHUB_SETUP_STEPS
    assert "Set up job" in _GITHUB_SETUP_STEPS


# ── rerun_failed_jobs ───────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


async def test_rerun_accepts_202(monkeypatch):
    seen: dict = {}

    async def fake_request(method, url, headers, **kwargs):
        seen["method"] = method
        seen["url"] = url
        return _Resp(202)

    monkeypatch.setattr(gh_module, "_github_request", fake_request)
    assert await rerun_failed_jobs("tok", "o", "r", 123) is True
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/actions/runs/123/rerun-failed-jobs")


async def test_rerun_rejects_non_accept(monkeypatch):
    async def fake_request(method, url, headers, **kwargs):
        return _Resp(403, "forbidden")

    monkeypatch.setattr(gh_module, "_github_request", fake_request)
    assert await rerun_failed_jobs("tok", "o", "r", 123) is False


async def test_rerun_swallows_exception(monkeypatch):
    async def fake_request(method, url, headers, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(gh_module, "_github_request", fake_request)
    assert await rerun_failed_jobs("tok", "o", "r", 123) is False


async def test_rerun_outcome_distinguishes_rejection_from_ambiguity(monkeypatch):
    async def rejected_request(method, url, headers, **kwargs):
        return _Resp(403, "forbidden")

    monkeypatch.setattr(gh_module, "_github_request", rejected_request)
    assert await rerun_failed_jobs_outcome("tok", "o", "r", 123) == "rejected"

    async def ambiguous_request(method, url, headers, **kwargs):
        raise RuntimeError("response lost")

    monkeypatch.setattr(gh_module, "_github_request", ambiguous_request)
    assert await rerun_failed_jobs_outcome("tok", "o", "r", 123) == "ambiguous"
