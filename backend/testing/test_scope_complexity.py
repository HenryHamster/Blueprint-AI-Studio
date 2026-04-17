"""Unit tests for app.scope_complexity._composite_complexity_score.

Standalone (no DB): only imports the scoring function and monkeypatches
its co-change cluster helper.
"""
from unittest.mock import patch

from app.scope_complexity import _composite_complexity_score


def _score(files, direction=None, cochange_index=None):
    return _composite_complexity_score(files, direction, cochange_index)


class TestFileCountPenalty:
    def test_no_penalty_up_to_two_files(self):
        s0, _ = _score([])
        s1, _ = _score(["pkg/sub/a.py"])
        s2, _ = _score(["pkg/sub/a.py", "pkg/sub/b.py"])
        assert s0 == 1.0
        assert s1 == 1.0
        assert s2 == 1.0

    def test_penalty_starts_at_three_files(self):
        s, meta = _score(["pkg/sub/a.py", "pkg/sub/b.py", "pkg/sub/c.py"])
        assert s == 1.0 - 0.08
        assert meta["files"] == 3

    def test_penalty_caps_at_point_three_two(self):
        # 0.08 * max(0, n-2) capped at 0.32 → cap reached at n >= 6
        s6, _ = _score([f"pkg/sub/f{i}.py" for i in range(6)])
        s10, _ = _score([f"pkg/sub/f{i}.py" for i in range(10)])
        assert abs(s6 - (1.0 - 0.32)) < 1e-9
        assert abs(s10 - (1.0 - 0.32)) < 1e-9


class TestExpansionRatioPenalty:
    def test_ratio_above_one_reduces_score(self):
        direction = {
            "research_history": [
                {"scope_breakdown": {"medium": {"avg_expansion_ratio": 1.5}}},
                {"scope_breakdown": {"medium": {"avg_expansion_ratio": 1.5}}},
            ]
        }
        s, meta = _score(["a.py"], direction=direction)
        # n=1 → no file penalty; ratio=1.5 → penalty = 0.12 * 0.5 = 0.06
        assert abs(s - (1.0 - 0.06)) < 1e-9
        assert meta["expansion_ratio"] == 1.5

    def test_ratio_at_or_below_one_no_penalty(self):
        direction = {
            "research_history": [
                {"scope_breakdown": {"medium": {"avg_expansion_ratio": 1.0}}},
            ]
        }
        s, _ = _score(["a.py"], direction=direction)
        assert s == 1.0

    def test_ratio_penalty_caps_at_point_three(self):
        direction = {
            "research_history": [
                {"scope_breakdown": {"medium": {"avg_expansion_ratio": 10.0}}},
            ]
        }
        s, _ = _score(["a.py"], direction=direction)
        assert abs(s - (1.0 - 0.3)) < 1e-9

    def test_ratio_ignored_when_no_files(self):
        direction = {
            "research_history": [
                {"scope_breakdown": {"medium": {"avg_expansion_ratio": 5.0}}},
            ]
        }
        s, meta = _score([], direction=direction)
        assert s == 1.0
        assert meta["expansion_ratio"] == 0.0

    def test_only_last_three_history_entries_used(self):
        # Older entries with huge ratio should be ignored; only last 3 used.
        direction = {
            "research_history": [
                {"scope_breakdown": {"medium": {"avg_expansion_ratio": 999.0}}},
                {"scope_breakdown": {"medium": {"avg_expansion_ratio": 1.0}}},
                {"scope_breakdown": {"medium": {"avg_expansion_ratio": 1.0}}},
                {"scope_breakdown": {"medium": {"avg_expansion_ratio": 1.0}}},
            ]
        }
        s, meta = _score(["a.py"], direction=direction)
        assert meta["expansion_ratio"] == 1.0
        assert s == 1.0


class TestCochangeClustering:
    def test_none_cluster_triggers_fallback_penalty(self):
        with patch(
            "app.scope_complexity._cluster_files_by_cochange", return_value=None
        ):
            s, _ = _score(
                ["pkg/sub/a.py", "pkg/sub/b.py"], cochange_index={"pkg/sub/a.py": {"pkg/sub/b.py": 1}}
            )
        # n=2 → no file penalty; single module; None cluster → -0.1
        assert abs(s - 0.9) < 1e-9

    def test_multi_cluster_penalty(self):
        with patch(
            "app.scope_complexity._cluster_files_by_cochange",
            return_value=[["pkg/sub/a.py"], ["pkg/sub/b.py"], ["pkg/sub/c.py"]],
        ):
            s, _ = _score(
                ["pkg/sub/a.py", "pkg/sub/b.py", "pkg/sub/c.py"],
                cochange_index={"pkg/sub/a.py": {"pkg/sub/b.py": 1}},
            )
        # n=3 → -0.08; clusters=3 → -0.08*2=0.16 penalty
        assert abs(s - (1.0 - 0.08 - 0.16)) < 1e-9

    def test_multi_cluster_penalty_caps(self):
        clusters = [[f"pkg/sub/f{i}.py"] for i in range(10)]
        with patch(
            "app.scope_complexity._cluster_files_by_cochange", return_value=clusters
        ):
            s, _ = _score(
                ["pkg/sub/a.py", "pkg/sub/b.py"],
                cochange_index={"pkg/sub/a.py": {"pkg/sub/b.py": 1}},
            )
        # cluster penalty capped at 0.24
        assert abs(s - (1.0 - 0.24)) < 1e-9

    def test_single_cluster_no_penalty(self):
        with patch(
            "app.scope_complexity._cluster_files_by_cochange",
            return_value=[["pkg/sub/a.py", "pkg/sub/b.py"]],
        ):
            s, _ = _score(
                ["pkg/sub/a.py", "pkg/sub/b.py"],
                cochange_index={"pkg/sub/a.py": {"pkg/sub/b.py": 1}},
            )
        assert s == 1.0

    def test_no_cochange_call_when_one_file(self):
        with patch(
            "app.scope_complexity._cluster_files_by_cochange", return_value=None
        ) as m:
            _score(["pkg/sub/a.py"], cochange_index={"pkg/sub/a.py": {"pkg/sub/b.py": 1}})
            assert m.call_count == 0

    def test_no_cochange_call_when_index_empty(self):
        with patch(
            "app.scope_complexity._cluster_files_by_cochange", return_value=None
        ) as m:
            _score(["pkg/sub/a.py", "pkg/sub/b.py"], cochange_index=None)
            assert m.call_count == 0


class TestCrossModuleSpan:
    def test_single_module_no_penalty(self):
        s, meta = _score(["backend/app/a.py", "backend/app/b.py"])
        assert s == 1.0
        assert meta["modules"] == 1

    def test_two_modules_penalty(self):
        s, meta = _score(["backend/app/a.py", "admin-ui/src/b.ts"])
        assert abs(s - (1.0 - 0.12)) < 1e-9
        assert meta["modules"] == 2

    def test_three_modules_scales_linearly(self):
        s, meta = _score(
            ["backend/app/a.py", "admin-ui/src/b.ts", "agent/src/c.ts"]
        )
        # n=3 file penalty 0.08 + module penalty 0.12*2=0.24
        assert abs(s - (1.0 - 0.08 - 0.24)) < 1e-9
        assert meta["modules"] == 3

    def test_module_penalty_caps(self):
        files = [f"mod{i}/sub/f.py" for i in range(10)]
        s, _ = _score(files)
        # file penalty maxed 0.32; module penalty capped at 0.3
        assert abs(s - max(0.0, 1.0 - 0.32 - 0.3)) < 1e-9


class TestClampingAndEmpty:
    def test_score_clamped_to_zero_floor(self):
        direction = {
            "research_history": [
                {"scope_breakdown": {"medium": {"avg_expansion_ratio": 100.0}}},
            ]
        }
        files = [f"mod{i}/sub/f.py" for i in range(20)]
        with patch(
            "app.scope_complexity._cluster_files_by_cochange",
            return_value=[[f] for f in files],
        ):
            s, _ = _score(files, direction=direction, cochange_index={"x": {"y": 1}})
        assert 0.0 <= s <= 1.0
        assert s == 0.0

    def test_score_clamped_upper_bound(self):
        s, _ = _score([])
        assert 0.0 <= s <= 1.0
        assert s == 1.0

    def test_empty_related_files(self):
        s, meta = _score([], direction=None, cochange_index=None)
        assert s == 1.0
        assert meta == {"files": 0, "expansion_ratio": 0.0, "modules": 0}

    def test_none_direction_and_cochange(self):
        s, meta = _score(
            ["pkg/sub/a.py", "pkg/sub/b.py", "pkg/sub/c.py"],
            direction=None,
            cochange_index=None,
        )
        assert abs(s - (1.0 - 0.08)) < 1e-9
        assert meta["expansion_ratio"] == 0.0

    def test_none_related_files_treated_as_empty(self):
        s, meta = _composite_complexity_score(None, None, None)  # type: ignore[arg-type]
        assert s == 1.0
        assert meta["files"] == 0
