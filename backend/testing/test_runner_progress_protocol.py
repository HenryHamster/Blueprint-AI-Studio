"""Interim-progress protocol (BLUEPRINT_PROGRESS_FILE) — runner-side tail reader,
isolation env injection, server-side merge validation, and the compute_result
progress summary.

The runner-side pieces (``scripts/local_runner.py`` / ``scripts/runner_isolation.py``)
are imported by path when the scripts tree is available (local checkouts, the
Docker harness with the repo mounted at ``/repo``) and skipped otherwise —
mirroring ``test_local_runner_terminal_log.py``. Server-side pieces
(``app.routers.runner`` merge helpers, ``app.task_completion`` summarizer) are
plain imports with no DB dependency.
"""

import ast
import os
import sys

import pytest


def _resolve_scripts_dir() -> "str | None":
    """Locate the repo's ``scripts/`` dir across local + Docker test environments.

    Candidates, in order: explicit env override, the ``/repo`` bind-mount
    (``scripts/test-backend.sh`` mounts the repo root read-only at ``/repo``
    in its Docker harness), then the repo root two levels above this file
    (plain checkouts and non-Docker runs).
    """
    candidates = []
    env_override = os.getenv("BLUEPRINT_SCRIPTS_DIR")
    if env_override:
        candidates.append(env_override)
    candidates.append("/repo/scripts")
    candidates.append(
        os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
    )
    for path in candidates:
        if os.path.isfile(os.path.join(path, "local_runner.py")):
            return path
    return None


def _require_scripts_dir() -> str:
    """Resolve the scripts dir or skip — except in CI, where a miss FAILS loudly.

    A silent skip in the promotion CI means the runner-side protocol checks
    never enforce; only ad-hoc environments without the checkout may skip.
    """
    scripts_dir = _resolve_scripts_dir()
    if scripts_dir is not None:
        return scripts_dir
    msg = (
        "scripts/ not available in this test environment "
        "(checked BLUEPRINT_SCRIPTS_DIR, the /repo bind-mount that "
        "scripts/test-backend.sh provides, and ../../scripts relative to "
        "this file)"
    )
    if os.getenv("CI"):
        pytest.fail(
            msg + " — refusing to skip in CI: mount the repo root at /repo "
            "or set BLUEPRINT_SCRIPTS_DIR so these invariants enforce"
        )
    pytest.skip(msg)


def _import_runner_modules():
    """Import (local_runner, runner_isolation) from the scripts tree, or skip."""
    scripts_dir = _require_scripts_dir()
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        import local_runner
        import runner_isolation as iso
    except Exception as exc:  # pragma: no cover - environment-dependent
        if os.getenv("CI"):
            pytest.fail(f"runner scripts unimportable in CI: {exc}")
        pytest.skip(f"runner scripts unimportable here: {exc}")
    return local_runner, iso


# ── Runner-side: _parse_progress_record / _ProgressTail ──────────────────────


class TestProgressTailReader:
    def test_parse_record_validates_types_and_bounds(self):
        local_runner, _iso = _import_runner_modules()
        parse = local_runner._parse_progress_record
        ok = parse(b'{"step": 3, "metric": "loss", "value": 1.5, "ts": "t"}')
        assert ok == {"metric": "loss", "value": 1.5, "step": 3, "ts": "t"}
        # Optional fields omitted when absent/invalid; extras dropped.
        assert parse(b'{"metric": "m", "value": 1, "step": -1, "extra": "x"}') == {
            "metric": "m",
            "value": 1.0,
        }
        # Garbage of every shape → None, never a raise.
        for bad in (
            b"",
            b"not json",
            b"[1, 2]",
            b'{"metric": "m"}',
            b'{"value": 1.0}',
            b'{"metric": "", "value": 1.0}',
            b'{"metric": "m", "value": true}',
            b'{"metric": "m", "value": "1.0"}',
            b'{"metric": "m", "value": NaN}',
            b'{"metric": "m", "value": Infinity}',
            b"\xff\xfe binary garbage",
        ):
            assert parse(bad) is None, f"expected None for {bad!r}"

    def test_tail_tracks_offset_and_counts_across_polls(self, tmp_path):
        local_runner, _iso = _import_runner_modules()
        path = tmp_path / "progress.jsonl"
        tail = local_runner._ProgressTail(str(path))

        # File does not exist yet — no progress, no error.
        assert tail.poll() is None

        with open(path, "a", encoding="utf-8") as f:
            f.write('{"step": 1, "metric": "loss", "value": 3.0}\n')
            f.write('{"step": 2, "metric": "loss", "value": 2.5}\n')
        snap = tail.poll()
        assert snap["count"] == 2
        assert [r["step"] for r in snap["records"]] == [1, 2]

        # Append more (including a garbage line) — only new bytes are read.
        with open(path, "a", encoding="utf-8") as f:
            f.write("garbage line\n")
            f.write('{"step": 3, "metric": "loss", "value": 2.0}\n')
        snap = tail.poll()
        assert snap["count"] == 3, "garbage lines must not count"
        assert [r["step"] for r in snap["records"]] == [1, 2, 3]

    def test_tail_carries_partial_line_between_polls(self, tmp_path):
        local_runner, _iso = _import_runner_modules()
        path = tmp_path / "progress.jsonl"
        tail = local_runner._ProgressTail(str(path))
        record = '{"step": 1, "metric": "loss", "value": 3.0}\n'
        with open(path, "a", encoding="utf-8") as f:
            f.write(record[:20])  # incomplete line
        assert tail.poll() is None
        with open(path, "a", encoding="utf-8") as f:
            f.write(record[20:])  # completion arrives next interval
        snap = tail.poll()
        assert snap["count"] == 1
        assert snap["records"][0]["value"] == 3.0

    def test_tail_resets_on_truncation_instead_of_freezing(self, tmp_path):
        """A file that shrinks below the tracked offset (truncated/rewritten
        by the job) must reset the reader to the top so progress resumes."""
        local_runner, _iso = _import_runner_modules()
        path = tmp_path / "progress.jsonl"
        tail = local_runner._ProgressTail(str(path))
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"step": 1, "metric": "loss", "value": 3.0}\n')
            f.write('{"step": 2, "metric": "loss", "value": 2.5}\n')
        assert tail.poll()["count"] == 2

        # The job rewrites the file from scratch with SHORTER content.
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"step": 3, "metric": "loss", "value": 2.0}\n')
        snap = tail.poll()
        assert snap["count"] == 3, "reader must resume after truncation, not freeze"
        assert snap["records"][-1]["step"] == 3

    def test_truncation_drops_stale_partial_line_carry(self, tmp_path):
        local_runner, _iso = _import_runner_modules()
        path = tmp_path / "progress.jsonl"
        tail = local_runner._ProgressTail(str(path))
        record = '{"step": 1, "metric": "loss", "value": 3.0}\n'
        with open(path, "a", encoding="utf-8") as f:
            f.write(record[:20])  # incomplete line, carried as partial
        assert tail.poll() is None

        # Truncate to empty: the carried partial belongs to bytes that no
        # longer exist and must be dropped, not glued onto the new content.
        with open(path, "w", encoding="utf-8"):
            pass
        assert tail.poll() is None
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"step": 7, "metric": "loss", "value": 1.0}\n')
        snap = tail.poll()
        assert snap["count"] == 1
        assert snap["records"][0]["step"] == 7

    def test_tail_bounds_records_and_skips_oversized_lines(self, tmp_path):
        local_runner, _iso = _import_runner_modules()
        path = tmp_path / "progress.jsonl"
        tail = local_runner._ProgressTail(str(path))
        with open(path, "a", encoding="utf-8") as f:
            # One hostile line far over the per-line cap...
            f.write('{"metric": "m", "value": 1.0, "pad": "' + "A" * 20000 + '"}\n')
            # ...followed by healthy records well over the tail cap.
            for step in range(100):
                f.write(f'{{"step": {step}, "metric": "loss", "value": {100 - step}}}\n')
        snap = tail.poll()
        assert snap["count"] == 100, "oversized line dropped, healthy lines counted"
        assert len(snap["records"]) <= local_runner._PROGRESS_TAIL_MAX_RECORDS
        assert snap["records"][-1]["step"] == 99
        # Serialized tail respects the byte budget (single-record floor).
        import json as _json

        assert (
            len(snap["records"]) == 1
            or len(_json.dumps(snap["records"])) <= local_runner._PROGRESS_TAIL_MAX_BYTES
        )


# ── Isolation: BLUEPRINT_PROGRESS_FILE env injection ─────────────────────────


class TestProgressEnvInjection:
    def _plan(self, iso, caps, image, progress=True, **over):
        kwargs = {
            "caps": caps,
            "command": "python train.py",
            "image": image,
            "registry_auth": None,
            "workspace_host": "/tmp/bp_x",
            "result_file_host": "/tmp/bp_x/result.json",
            "artifact_dir_host": "/tmp/bp_x/artifacts",
            "progress_file_host": "/tmp/bp_x/progress.jsonl" if progress else None,
            "clone_dir_host": None,
            "cwd_host": None,
            "job_env": {},
            "gpu_indices": [],
            "container_name": "bp-test",
            "source_env": {"PATH": "/usr/bin"},
        }
        kwargs.update(over)
        return iso.plan_compute_execution(**kwargs)

    def test_bwrap_injects_translated_progress_path(self):
        _local_runner, iso = _import_runner_modules()
        caps = iso.Capabilities(
            bwrap_path="/usr/bin/bwrap", unshare_path="/usr/bin/unshare", userns_ok=True
        )
        plan = self._plan(iso, caps, image=None)
        assert plan.backend == iso.BACKEND_BWRAP
        assert plan.env["BLUEPRINT_PROGRESS_FILE"] == "/workspace/progress.jsonl"

    def test_docker_injects_out_mount_progress_path(self):
        _local_runner, iso = _import_runner_modules()
        caps = iso.Capabilities(docker_usable=True)
        plan = self._plan(iso, caps, image="nvcr.io/nvidia/jax:latest")
        assert plan.backend == iso.BACKEND_DOCKER
        assert plan.env["BLUEPRINT_PROGRESS_FILE"] == "/tmp/blueprint_out/progress.jsonl"

    def test_absent_progress_file_omits_env(self):
        """progress_file_host=None ⇒ no env var — byte-identical old behavior."""
        _local_runner, iso = _import_runner_modules()
        caps = iso.Capabilities(
            bwrap_path="/usr/bin/bwrap", unshare_path="/usr/bin/unshare", userns_ok=True
        )
        plan = self._plan(iso, caps, image=None, progress=False)
        assert "BLUEPRINT_PROGRESS_FILE" not in plan.env


# ── Runner-side AST invariants (no import needed) ────────────────────────────


def _load_local_runner_tree() -> ast.Module:
    scripts_dir = _require_scripts_dir()
    with open(os.path.join(scripts_dir, "local_runner.py"), encoding="utf-8") as f:
        return ast.parse(f.read())


def test_interim_running_report_carries_progress_and_log_tail():
    """The in-loop interim report must send progress + live log tail; the
    terminal kill paths must send progress alongside their command_log.

    Terminal reports flow through the nested _report_terminal wrapper which
    forwards all kwargs to _report_compute_job; this test counts both the
    direct running-report call sites and the wrapper call sites with progress.
    """
    tree = _load_local_runner_tree()
    run_job = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_run_compute_job"
        ),
        None,
    )
    assert run_job is not None

    # Find the nested _report_terminal wrapper so we can verify it forwards progress.
    report_terminal_def = next(
        (
            node
            for node in ast.walk(run_job)
            if isinstance(node, ast.FunctionDef) and node.name == "_report_terminal"
        ),
        None,
    )

    running_with_progress = 0
    terminal_with_progress = 0
    for node in ast.walk(run_job):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        kwargs = {kw.arg for kw in node.keywords}
        func_name = node.func.id

        if func_name == "_report_compute_job":
            status_kw = next(
                (kw for kw in node.keywords if kw.arg == "exec_status"), None
            )
            status = (
                status_kw.value.value
                if status_kw is not None and isinstance(status_kw.value, ast.Constant)
                else None
            )
            if status == "running" and "progress" in kwargs and "command_log" in kwargs:
                running_with_progress += 1

        elif func_name == "_report_terminal" and "progress" in kwargs:
            # Terminal kill paths now delegate to the _report_terminal wrapper;
            # count wrapper call sites that carry progress.
            terminal_with_progress += 1

    assert running_with_progress >= 1, (
        "the periodic running report must carry progress + command_log tail"
    )
    assert terminal_with_progress >= 3, (
        "deadline/shutdown/cancel + final terminal reports must carry progress"
    )
    # Verify the wrapper itself forwards progress through to _report_compute_job.
    assert report_terminal_def is not None, (
        "_report_terminal wrapper not found inside _run_compute_job"
    )
    wrapper_forwards_progress = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_report_compute_job"
        and "progress" in {kw.arg for kw in node.keywords}
        for node in ast.walk(report_terminal_def)
    )
    assert wrapper_forwards_progress, (
        "_report_terminal wrapper does not forward progress to _report_compute_job"
    )


# ── Reaper: durable reason-history call-site invariant ───────────────────────


def test_reaper_orphan_loop_records_reason_history():
    """The orphan-reap loop must call _record_reap_reason_history after the
    relock (durable REAPED TaskHistory rows — reasons survive later reaps)."""
    reaper_path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "app", "reaper.py")
    )
    with open(reaper_path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    service = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "reaper_service"
        ),
        None,
    )
    assert service is not None, "reaper_service not found in reaper.py"
    calls = [
        node
        for node in ast.walk(service)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_record_reap_reason_history"
    ]
    assert calls, (
        "reaper_service no longer records durable REAPED reason history — "
        "reap reasons would again live only in mutable status messages"
    )


# ── Server-side: _merge_progress_report pure edge cases ──────────────────────


class TestMergeProgressReport:
    def _merge(self, existing, raw):
        from app.routers.runner import _merge_progress_report
        from app.models import _utcnow

        merged, _new_records = _merge_progress_report(existing, raw, _utcnow())
        return merged

    def test_invalid_count_treats_validated_records_as_new(self):
        merged = self._merge(
            None,
            {"records": [{"metric": "m", "value": 1.0}], "count": "lots"},
        )
        assert merged is not None
        assert merged["count"] == 1
        assert len(merged["records"]) == 1

    def test_absurd_count_is_bounded_and_appends_only_report_records(self):
        from app.routers.runner import _MAX_PROGRESS_COUNT

        merged = self._merge(
            {"records": [{"metric": "m", "value": 0.5}], "count": 1},
            {
                "records": [{"metric": "m", "value": 1.0}],
                "count": _MAX_PROGRESS_COUNT,
            },
        )
        assert merged is not None
        assert len(merged["records"]) == 2  # bounded append, no blow-up
        assert merged["count"] == _MAX_PROGRESS_COUNT

    def test_corrupt_existing_state_is_tolerated(self):
        merged = self._merge(
            {"records": "corrupt", "count": {"weird": True}},
            {"records": [{"metric": "m", "value": 1.0}], "count": 1},
        )
        assert merged is not None
        assert merged["records"] == [{"metric": "m", "value": 1.0}]

    def test_all_invalid_records_returns_none(self):
        assert (
            self._merge(None, {"records": [{"metric": 5, "value": None}], "count": 3})
            is None
        )


# ── compute_result progress summary (pure) ───────────────────────────────────


class TestSummarizeProgressRecords:
    def _summarize(self, progress, criterion=None):
        from app.task_completion import _summarize_progress_records

        return _summarize_progress_records(progress, criterion)

    def test_first_last_best_and_count(self):
        progress = {
            "records": [
                {"step": 1, "metric": "reward", "value": 5.0},
                {"step": 2, "metric": "reward", "value": 9.0},
                {"step": 3, "metric": "reward", "value": 7.0},
            ],
            "count": 40,  # 37 older records fell off the stored tail
        }
        summary = self._summarize(progress)
        assert summary["metric"] == "reward"
        assert summary["count"] == 40
        assert summary["first"] == {"value": 5.0, "step": 1}
        assert summary["last"] == {"value": 7.0, "step": 3}
        assert summary["best"] == {"value": 9.0, "step": 2}
        assert summary["best_direction"] == "max"

    def test_criterion_operator_flips_best_to_min(self):
        progress = {
            "records": [
                {"step": 1, "metric": "loss", "value": 3.0},
                {"step": 2, "metric": "loss", "value": 1.5},
                {"step": 3, "metric": "loss", "value": 2.0},
            ],
            "count": 3,
        }
        summary = self._summarize(
            progress, {"metric": "loss", "operator": "<=", "threshold": 1.0}
        )
        assert summary["best"] == {"value": 1.5, "step": 2}
        assert summary["best_direction"] == "min"

    def test_mixed_metrics_scope_to_last_series(self):
        progress = {
            "records": [
                {"step": 1, "metric": "lr", "value": 0.001},
                {"step": 1, "metric": "loss", "value": 3.0},
                {"step": 2, "metric": "loss", "value": 2.0},
            ],
            "count": 3,
        }
        summary = self._summarize(progress)
        assert summary["metric"] == "loss"
        assert summary["first"]["value"] == 3.0

    def test_garbage_progress_returns_none(self):
        for garbage in (
            None,
            "text",
            {},
            {"records": "nope"},
            {"records": []},
            {"records": [{"metric": "m", "value": float("nan")}]},
            {"records": [{"metric": None, "value": 1.0}]},
        ):
            assert self._summarize(garbage) is None, f"expected None for {garbage!r}"
