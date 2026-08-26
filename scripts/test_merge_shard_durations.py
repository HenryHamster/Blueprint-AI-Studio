"""Regression tests for scripts/merge_shard_durations.py.

The tool reconstructs backend/.test_durations by merging the per-shard
pytest-split duration files that the promotion PR's 8 shards already produce.
The invariants worth pinning are the ones whose violation degrades the artifact
*silently* -- a merged file that looks complete while being stale or truncated
still parses, still commits, and only shows up later as shard drift:

  * prefer-fresh      a freshly measured value beats the stale baseline value
  * no-drop           a test no shard measured keeps its baseline entry
  * carry-over        pytest-split's baseline echo never overwrites a fresh value
  * determinism       same inputs -> byte-identical output, any argument order
  * fail-closed       malformed/empty input aborts instead of writing a truncated map

Most tests pass ``allow_quality_failure=True`` because a 3-entry fixture cannot
clear the corpus-wide quality floor (n>=5000, unique>=100, CV>1) that
scripts/durations_quality.py enforces; the floor itself is pinned separately by
the quality tests below against a realistically shaped synthetic corpus.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import merge_shard_durations  # noqa: E402
from merge_shard_durations import (  # noqa: E402
    EXIT_OK,
    EXIT_WOULD_CHANGE,
    MergeError,
    merge_shards,
)


def _write_json(path: Path, data: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def _silent(*_args, **_kwargs) -> None:
    """Log sink so the merge does not spray fixture noise into pytest output."""


def _merge(
    tmp_path: Path,
    baseline: dict[str, float],
    shards: dict[str, dict[str, float] | None],
    **kwargs,
) -> tuple[int, dict[str, float]]:
    """Run a merge over freshly written fixtures; return (exit code, merged map).

    A shard mapped to ``None`` is deliberately NOT written, standing in for a
    shard whose CI job was skipped or whose artifact upload was lost.
    """
    baseline_path = tmp_path / ".test_durations"
    _write_json(baseline_path, baseline)
    shard_paths = []
    for name, payload in shards.items():
        shard_path = tmp_path / name
        if payload is not None:
            _write_json(shard_path, payload)
        shard_paths.append(str(shard_path))
    kwargs.setdefault("allow_quality_failure", True)
    code = merge_shards(
        shard_paths=shard_paths,
        baseline_path=str(baseline_path),
        log=_silent,
        **kwargs,
    )
    output = kwargs.get("output_path") or str(baseline_path)
    written = json.loads(Path(output).read_text(encoding="utf-8"))
    return code, written


def _quality_corpus(prefix: str = "testing/test_corpus.py::test_") -> dict[str, float]:
    """A synthetic corpus shaped like the real artifact: clears the quality floor.

    Needs n>=5000 entries, >=100 distinct values and CV>1, so the bulk of the
    mass sits in a dense sub-second band with a sparse multi-second tail --
    the same shape backend/.test_durations actually has.
    """
    corpus: dict[str, float] = {}
    for index in range(5200):
        if index % 40:
            value = round(0.001 * ((index % 400) + 1), 6)
        else:
            value = round(5.0 + (index % 97) * 0.01, 6)
        corpus[f"{prefix}{index}"] = value
    return corpus


# --------------------------------------------------------------------------
# normal merge
# --------------------------------------------------------------------------


def test_fresh_shard_value_replaces_stale_baseline_value(tmp_path: Path) -> None:
    """prefer-fresh: a re-measured test takes the shard's value, not the baseline's."""
    code, merged = _merge(
        tmp_path,
        baseline={"testing/test_a.py::test_one": 9.0},
        shards={"shard-1.json": {"testing/test_a.py::test_one": 1.25}},
    )
    assert code == EXIT_OK
    assert merged["testing/test_a.py::test_one"] == 1.25


def test_merges_several_shards_into_one_map(tmp_path: Path) -> None:
    code, merged = _merge(
        tmp_path,
        baseline={"testing/test_a.py::test_one": 9.0, "testing/test_b.py::test_two": 9.0},
        shards={
            "shard-1.json": {"testing/test_a.py::test_one": 1.0},
            "shard-2.json": {"testing/test_b.py::test_two": 2.0},
        },
    )
    assert code == EXIT_OK
    assert merged == {
        "testing/test_a.py::test_one": 1.0,
        "testing/test_b.py::test_two": 2.0,
    }


def test_test_present_in_shard_but_not_baseline_is_added(tmp_path: Path) -> None:
    """A newly added test must enter the artifact, else it is never time-balanced."""
    code, merged = _merge(
        tmp_path,
        baseline={"testing/test_a.py::test_one": 1.0},
        shards={"shard-1.json": {"testing/test_new.py::test_brand_new": 3.5}},
    )
    assert code == EXIT_OK
    assert merged["testing/test_new.py::test_brand_new"] == 3.5
    assert merged["testing/test_a.py::test_one"] == 1.0


# --------------------------------------------------------------------------
# no-drop: unmeasured entries survive
# --------------------------------------------------------------------------


def test_baseline_entry_no_shard_measured_is_preserved(tmp_path: Path) -> None:
    """no-drop: dropping unmeasured tests is how a durations file quietly rots."""
    code, merged = _merge(
        tmp_path,
        baseline={
            "testing/test_a.py::test_measured": 9.0,
            "testing/test_b.py::test_unmeasured": 7.5,
        },
        shards={"shard-1.json": {"testing/test_a.py::test_measured": 1.0}},
    )
    assert code == EXIT_OK
    assert merged["testing/test_b.py::test_unmeasured"] == 7.5, (
        "a test no shard measured must keep its baseline duration"
    )


def test_missing_shard_file_is_tolerated_and_its_tests_keep_baseline(
    tmp_path: Path,
) -> None:
    """A skipped shard must not blow up the merge nor erase the tests it owned."""
    code, merged = _merge(
        tmp_path,
        baseline={
            "testing/test_a.py::test_one": 9.0,
            "testing/test_b.py::test_two": 8.0,
        },
        shards={
            "shard-1.json": {"testing/test_a.py::test_one": 1.0},
            "shard-2.json": None,  # never written
        },
    )
    assert code == EXIT_OK
    assert merged["testing/test_a.py::test_one"] == 1.0
    assert merged["testing/test_b.py::test_two"] == 8.0


def test_all_shards_absent_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(MergeError, match="absent"):
        _merge(
            tmp_path,
            baseline={"testing/test_a.py::test_one": 1.0},
            shards={"shard-1.json": None, "shard-2.json": None},
        )


# --------------------------------------------------------------------------
# the pytest-split carry-over trap
# --------------------------------------------------------------------------


def test_baseline_carry_over_never_overwrites_a_fresh_measurement(
    tmp_path: Path,
) -> None:
    """pytest-split seeds its cache from --durations-path and writes it back.

    So a shard pointed at a populated durations file emits the FULL map, with
    only its own group fresh and everything else a byte-exact baseline echo.
    Merging those naively lets one shard's echo clobber another's measurement.
    Here shard-2 echoes the stale 9.0 for the test shard-1 actually measured.
    """
    baseline = {
        "testing/test_a.py::test_one": 9.0,
        "testing/test_b.py::test_two": 8.0,
    }
    code, merged = _merge(
        tmp_path,
        baseline=baseline,
        shards={
            "shard-1.json": {
                "testing/test_a.py::test_one": 1.0,  # freshly measured here
                "testing/test_b.py::test_two": 8.0,  # echo of the baseline
            },
            "shard-2.json": {
                "testing/test_a.py::test_one": 9.0,  # echo of the baseline
                "testing/test_b.py::test_two": 2.0,  # freshly measured here
            },
        },
    )
    assert code == EXIT_OK
    assert merged["testing/test_a.py::test_one"] == 1.0, "stale echo clobbered a fresh value"
    assert merged["testing/test_b.py::test_two"] == 2.0, "stale echo clobbered a fresh value"


# --------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------


def test_two_runs_are_byte_identical(tmp_path: Path) -> None:
    baseline = {f"testing/test_a.py::test_{i}": float(i) for i in range(50)}
    shards = {
        "shard-1.json": {f"testing/test_a.py::test_{i}": i + 0.5 for i in range(0, 50, 2)},
        "shard-2.json": {f"testing/test_a.py::test_{i}": i + 0.25 for i in range(1, 50, 2)},
    }
    first = tmp_path / "first"
    second = tmp_path / "second"
    for target in (first, second):
        _merge(target, baseline=baseline, shards=dict(shards))
    assert (first / ".test_durations").read_bytes() == (
        second / ".test_durations"
    ).read_bytes()


def test_output_does_not_depend_on_shard_argument_order(tmp_path: Path) -> None:
    """Glob/matrix ordering must not change the artifact, or guards flap.

    ``test_shared`` is deliberately measured fresh by BOTH shards with different
    values -- a retry or an overlapping re-run does this. Resolving that
    collision by "last file wins" would make the artifact depend on argument
    order; the merge takes the max instead, which is order-independent.
    """
    baseline = {
        "testing/test_a.py::test_one": 9.0,
        "testing/test_b.py::test_two": 8.0,
        "testing/test_c.py::test_shared": 7.0,
    }
    payloads = {
        "shard-1.json": {
            "testing/test_a.py::test_one": 1.0,
            "testing/test_c.py::test_shared": 3.0,
        },
        "shard-2.json": {
            "testing/test_b.py::test_two": 2.0,
            "testing/test_c.py::test_shared": 4.0,
        },
    }
    forward = tmp_path / "forward"
    reverse = tmp_path / "reverse"
    _merge(forward, baseline=baseline, shards=dict(payloads))
    _merge(reverse, baseline=baseline, shards=dict(reversed(list(payloads.items()))))
    assert (forward / ".test_durations").read_bytes() == (
        reverse / ".test_durations"
    ).read_bytes()


def test_rendered_format_matches_the_committed_artifact_convention() -> None:
    """sort_keys + indent 2 + trailing newline, same as scripts/merge_durations.py.

    Pinned literally: a formatting drift would make every merge produce a
    whole-file diff and defeat review of the real duration changes.
    """
    rendered = merge_shard_durations.render({"b": 2.0, "a": 1.0})
    assert rendered == '{\n  "a": 1.0,\n  "b": 2.0\n}\n'


# --------------------------------------------------------------------------
# fail-closed on bad input
# --------------------------------------------------------------------------


def test_corrupt_shard_file_fails_closed_and_leaves_artifact_untouched(
    tmp_path: Path,
) -> None:
    baseline_path = tmp_path / ".test_durations"
    _write_json(baseline_path, {"testing/test_a.py::test_one": 1.0})
    before = baseline_path.read_bytes()
    shard = tmp_path / "shard-1.json"
    shard.write_text('{"testing/test_a.py::test_one": 1.0', encoding="utf-8")

    with pytest.raises(MergeError, match="malformed JSON"):
        merge_shards(
            shard_paths=[str(shard)],
            baseline_path=str(baseline_path),
            allow_quality_failure=True,
            log=_silent,
        )
    assert baseline_path.read_bytes() == before, "artifact was modified despite failure"


def test_empty_shard_file_fails_closed(tmp_path: Path) -> None:
    """An empty map means the shard recorded nothing -- do not treat it as data."""
    with pytest.raises(MergeError, match="empty"):
        _merge(
            tmp_path,
            baseline={"testing/test_a.py::test_one": 1.0},
            shards={"shard-1.json": {}},
        )


def test_shard_that_is_a_json_list_fails_closed(tmp_path: Path) -> None:
    baseline_path = tmp_path / ".test_durations"
    _write_json(baseline_path, {"testing/test_a.py::test_one": 1.0})
    shard = tmp_path / "shard-1.json"
    shard.write_text("[]", encoding="utf-8")
    with pytest.raises(MergeError, match="expected a JSON object"):
        merge_shards(
            shard_paths=[str(shard)],
            baseline_path=str(baseline_path),
            allow_quality_failure=True,
            log=_silent,
        )


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_duration_fails_closed(tmp_path: Path, literal: str) -> None:
    """json.loads accepts NaN/Infinity by default; they must not reach the artifact."""
    baseline_path = tmp_path / ".test_durations"
    _write_json(baseline_path, {"testing/test_a.py::test_one": 1.0})
    shard = tmp_path / "shard-1.json"
    shard.write_text(
        f'{{"testing/test_a.py::test_one": {literal}}}', encoding="utf-8"
    )
    with pytest.raises(MergeError, match="non-finite"):
        merge_shards(
            shard_paths=[str(shard)],
            baseline_path=str(baseline_path),
            allow_quality_failure=True,
            log=_silent,
        )


def test_negative_duration_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(MergeError, match="negative"):
        _merge(
            tmp_path,
            baseline={"testing/test_a.py::test_one": 1.0},
            shards={"shard-1.json": {"testing/test_a.py::test_one": -3.0}},
        )


def test_non_numeric_duration_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(MergeError, match="non-numeric"):
        _merge(
            tmp_path,
            baseline={"testing/test_a.py::test_one": 1.0},
            shards={"shard-1.json": {"testing/test_a.py::test_one": "slow"}},
        )


def test_missing_baseline_fails_closed(tmp_path: Path) -> None:
    shard = tmp_path / "shard-1.json"
    _write_json(shard, {"testing/test_a.py::test_one": 1.0})
    with pytest.raises(MergeError, match="cannot read"):
        merge_shards(
            shard_paths=[str(shard)],
            baseline_path=str(tmp_path / "nope.json"),
            allow_quality_failure=True,
            log=_silent,
        )


# --------------------------------------------------------------------------
# duration policy reuse + quality floor
# --------------------------------------------------------------------------


def test_outlier_above_cap_is_dropped(tmp_path: Path) -> None:
    """Fixture cold-start misattribution must not enter the artifact.

    OUTLIER_CAP is 60.0s in scripts/durations_quality.py; 600s is a Postgres
    cold start billed onto the first test of a batch, not a real test cost.
    """
    code, merged = _merge(
        tmp_path,
        baseline={"testing/test_a.py::test_one": 1.0},
        shards={
            "shard-1.json": {
                "testing/test_a.py::test_one": 2.0,
                "testing/test_slow.py::test_cold_start": 600.0,
            }
        },
    )
    assert code == EXIT_OK
    assert "testing/test_slow.py::test_cold_start" not in merged
    assert merged["testing/test_a.py::test_one"] == 2.0


def test_quality_floor_failure_refuses_to_write(tmp_path: Path) -> None:
    """A merge that would degrade the corpus below the floor must not land."""
    baseline_path = tmp_path / ".test_durations"
    _write_json(baseline_path, {"testing/test_a.py::test_one": 1.0})
    before = baseline_path.read_bytes()
    shard = tmp_path / "shard-1.json"
    _write_json(shard, {"testing/test_a.py::test_one": 2.0})

    with pytest.raises(MergeError, match="quality floor"):
        merge_shards(
            shard_paths=[str(shard)],
            baseline_path=str(baseline_path),
            log=_silent,
        )
    assert baseline_path.read_bytes() == before


def test_realistic_corpus_clears_the_quality_floor(tmp_path: Path) -> None:
    """The happy path must pass the floor without the override flag."""
    corpus = _quality_corpus()
    shard = {key: value + 0.000123 for key, value in list(corpus.items())[:2600]}
    baseline_path = tmp_path / ".test_durations"
    _write_json(baseline_path, corpus)
    shard_path = tmp_path / "shard-1.json"
    _write_json(shard_path, shard)

    code = merge_shards(
        shard_paths=[str(shard_path)],
        baseline_path=str(baseline_path),
        log=_silent,
    )
    assert code == EXIT_OK
    merged = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert len(merged) == len(corpus)
    values = list(merged.values())
    assert statistics.stdev(values) / statistics.mean(values) > 1.0


# --------------------------------------------------------------------------
# --check mode
# --------------------------------------------------------------------------


def test_check_mode_reports_change_without_writing(tmp_path: Path) -> None:
    baseline_path = tmp_path / ".test_durations"
    _write_json(baseline_path, {"testing/test_a.py::test_one": 9.0})
    before = baseline_path.read_bytes()
    shard = tmp_path / "shard-1.json"
    _write_json(shard, {"testing/test_a.py::test_one": 1.0})

    code = merge_shards(
        shard_paths=[str(shard)],
        baseline_path=str(baseline_path),
        check=True,
        allow_quality_failure=True,
        log=_silent,
    )
    assert code == EXIT_WOULD_CHANGE
    assert baseline_path.read_bytes() == before, "--check must not write"


def test_check_mode_reports_up_to_date_when_already_converged(tmp_path: Path) -> None:
    """Re-running the merge on its own output is a no-op, so a guard stays green."""
    baseline = {"testing/test_a.py::test_one": 9.0, "testing/test_b.py::test_two": 8.0}
    shards = {"shard-1.json": {"testing/test_a.py::test_one": 1.0}}
    _merge(tmp_path, baseline=baseline, shards=dict(shards))

    code = merge_shards(
        shard_paths=[str(tmp_path / "shard-1.json")],
        baseline_path=str(tmp_path / ".test_durations"),
        check=True,
        allow_quality_failure=True,
        log=_silent,
    )
    assert code == EXIT_OK


# --------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------


def test_cli_writes_merged_artifact(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    baseline_path = tmp_path / ".test_durations"
    _write_json(baseline_path, {"testing/test_a.py::test_one": 9.0})
    shard = tmp_path / "shard-1.json"
    _write_json(shard, {"testing/test_a.py::test_one": 1.0})

    code = merge_shard_durations.main(
        [str(shard), "--baseline", str(baseline_path), "--allow-quality-failure"]
    )
    capsys.readouterr()
    assert code == EXIT_OK
    merged = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert merged["testing/test_a.py::test_one"] == 1.0


def test_cli_returns_error_exit_code_on_corrupt_shard(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    baseline_path = tmp_path / ".test_durations"
    _write_json(baseline_path, {"testing/test_a.py::test_one": 1.0})
    shard = tmp_path / "shard-1.json"
    shard.write_text("not json at all", encoding="utf-8")

    code = merge_shard_durations.main(
        [str(shard), "--baseline", str(baseline_path), "--allow-quality-failure"]
    )
    captured = capsys.readouterr()
    assert code == merge_shard_durations.EXIT_ERROR
    assert "ERROR" in captured.err


def test_cli_output_flag_leaves_baseline_untouched(tmp_path: Path) -> None:
    baseline_path = tmp_path / ".test_durations"
    _write_json(baseline_path, {"testing/test_a.py::test_one": 9.0})
    before = baseline_path.read_bytes()
    shard = tmp_path / "shard-1.json"
    _write_json(shard, {"testing/test_a.py::test_one": 1.0})
    out = tmp_path / "merged.json"

    code = merge_shard_durations.main(
        [
            str(shard),
            "--baseline",
            str(baseline_path),
            "--output",
            str(out),
            "--allow-quality-failure",
        ]
    )
    assert code == EXIT_OK
    assert baseline_path.read_bytes() == before
    assert json.loads(out.read_text(encoding="utf-8"))["testing/test_a.py::test_one"] == 1.0
