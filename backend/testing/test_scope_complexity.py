"""Unit tests for app.scope_complexity._composite_complexity_score.

Standalone (no DB): only imports the scoring function and monkeypatches
its co-change cluster helper.

Scoring is multiplicative: each dimension contributes a factor in
[0.5, 1.0], and the composite is the product. Moderate values across
dimensions should not cascade below the 0.45 escalation threshold.
"""
from unittest.mock import patch

from app.scope_complexity import _composite_complexity_score


def _score(files, direction=None, cochange_index=None):
    return _composite_complexity_score(files, direction, cochange_index)


class TestFileCountFactor:
    def test_no_penalty_up_to_two_files(self):
        for files in ([], ["pkg/sub/a.py"], ["pkg/sub/a.py", "pkg/sub/b.py"]):
            s, _ = _score(files)
            assert s == 1.0

    def test_penalty_starts_at_three_files(self):
        s, meta = _score(["pkg/sub/a.py", "pkg/sub/b.py", "pkg/sub/c.py"])
        assert abs(s - (1.0 - 0.0625)) < 1e-9
        assert meta["files"] == 3
        assert meta["factors"]["files"] == round(1.0 - 0.0625, 3)

    def test_file_factor_floors_at_half(self):
        s, _ = _score([f"pkg/sub/f{i}.py" for i in range(20)])
        assert s == 0.5


class TestExpansionRatioFactor:
    def test_ratio_above_one_reduces_score(self):
        direction = {
            "research_history": [
                {"scope_breakdown": {"medium": {"avg_expansion_ratio": 1.5}}},
                {"scope_breakdown": {"medium": {"avg_expansion_ratio": 1.5}}},
            ]
        }
        s, meta = _score(["a.py"], direction=direction)
        # n=1 no file penalty; ratio=1.5 → factor = 1 - 0.125*0.5 = 0.9375
        assert abs(s - 0.9375) < 1e-9
        assert meta["expansion_ratio"] == 1.5

    def test_ratio_at_or_below_one_no_penalty(self):
        direction = {
            "research_history": [
                {"scope_breakdown": {"medium": {"avg_expansion_ratio": 1.0}}},
            ]
        }
        s, _ = _score(["a.py"], direction=direction)
        assert s == 1.0

    def test_ratio_factor_floors_at_half(self):
        direction = {
            "research_history": [
                {"scope_breakdown": {"medium": {"avg_expansion_ratio": 10.0}}},
            ]
        }
        s, _ = _score(["a.py"], direction=direction)
        assert s == 0.5

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


class TestCochangeFactor:
    def test_none_cluster_applies_fallback_factor(self):
        with patch(
            "app.scope_complexity._cluster_files_by_cochange", return_value=None
        ):
            s, _ = _score(
                ["pkg/sub/a.py", "pkg/sub/b.py"],
                cochange_index={"pkg/sub/a.py": {"pkg/sub/b.py": 1}},
            )
        assert abs(s - 0.9) < 1e-9

    def test_multi_cluster_factor(self):
        with patch(
            "app.scope_complexity._cluster_files_by_cochange",
            return_value=[["pkg/sub/a.py"], ["pkg/sub/b.py"], ["pkg/sub/c.py"]],
        ):
            s, _ = _score(
                ["pkg/sub/a.py", "pkg/sub/b.py", "pkg/sub/c.py"],
                cochange_index={"pkg/sub/a.py": {"pkg/sub/b.py": 1}},
            )
        # f_files = 1 - 0.0625 = 0.9375; f_clusters = 1 - 0.08*2 = 0.84
        assert abs(s - (0.9375 * 0.84)) < 1e-9

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


class TestNeighborFactor:
    def test_no_unlisted_neighbors_no_penalty(self):
        with patch(
            "app.scope_complexity._cluster_files_by_cochange",
            return_value=[["pkg/sub/a.py", "pkg/sub/b.py"]],
        ):
            s, meta = _score(
                ["pkg/sub/a.py", "pkg/sub/b.py"],
                cochange_index={"pkg/sub/a.py": {"pkg/sub/b.py": 1.0}},
            )
        assert s == 1.0
        assert meta["neighbor_ratio"] == 0.0
        assert meta["factors"]["neighbors"] == 1.0

    def test_unlisted_neighbors_at_ratio_one(self):
        # 1 listed file with 1 unlisted neighbor (ratio=1.0) → f = 1 - 0.5/3 ≈ 0.833
        with patch(
            "app.scope_complexity._cluster_files_by_cochange", return_value=None,
        ):
            s, meta = _score(
                ["a.py"],
                cochange_index={"a.py": {"x.py": 1.0}},
            )
        assert meta["neighbor_ratio"] == 1.0
        assert abs(meta["factors"]["neighbors"] - round(1.0 - 0.5 / 3.0, 3)) < 1e-9
        assert abs(s - (1.0 - 0.5 / 3.0)) < 1e-9

    def test_neighbor_factor_floors_at_half(self):
        with patch(
            "app.scope_complexity._cluster_files_by_cochange", return_value=None,
        ):
            s, meta = _score(
                ["a.py"],
                cochange_index={"a.py": {f"x{i}.py": 1.0 for i in range(5)}},
            )
        assert meta["neighbor_ratio"] == 5.0
        assert meta["factors"]["neighbors"] == 0.5
        assert s == 0.5

    def test_low_affinity_partners_ignored(self):
        with patch(
            "app.scope_complexity._cluster_files_by_cochange", return_value=None,
        ):
            s, meta = _score(
                ["a.py"],
                cochange_index={"a.py": {"x.py": 0.9, "y.py": 0.5}},
            )
        assert meta["neighbor_ratio"] == 0.0
        assert s == 1.0


class TestCrossModuleFactor:
    def test_single_module_no_penalty(self):
        s, meta = _score(["backend/app/a.py", "backend/app/b.py"])
        assert s == 1.0
        assert meta["modules"] == 1

    def test_two_modules_factor(self):
        s, meta = _score(["backend/app/a.py", "admin-ui/src/b.ts"])
        # f_modules = 1 - 0.125 = 0.875
        assert abs(s - 0.875) < 1e-9
        assert meta["modules"] == 2

    def test_three_modules(self):
        s, meta = _score(
            ["backend/app/a.py", "admin-ui/src/b.ts", "agent/src/c.ts"]
        )
        # f_files = 1 - 0.0625 = 0.9375; f_modules = 1 - 0.125*2 = 0.75
        assert abs(s - (0.9375 * 0.75)) < 1e-9
        assert meta["modules"] == 3


class TestCompositeBehavior:
    def test_moderate_across_dimensions_stays_above_threshold(self):
        """5-file, 2-module finding with moderate expansion (1.5x) should
        stay at medium: composite must remain >= 0.45."""
        direction = {
            "research_history": [
                {"scope_breakdown": {"medium": {"avg_expansion_ratio": 1.5}}},
            ]
        }
        files = [
            "backend/app/a.py",
            "backend/app/b.py",
            "backend/app/c.py",
            "backend/app/d.py",
            "admin-ui/src/e.ts",
        ]
        s, _ = _score(files, direction=direction)
        assert s >= 0.45, f"moderate case escalated prematurely: score={s:.3f}"

    def test_extreme_across_dimensions_escalates(self):
        """10-file, 3-module finding with high expansion (3x) should
        escalate: composite must fall below 0.45."""
        direction = {
            "research_history": [
                {"scope_breakdown": {"medium": {"avg_expansion_ratio": 3.0}}},
            ]
        }
        files = (
            [f"backend/app/f{i}.py" for i in range(4)]
            + [f"admin-ui/src/g{i}.ts" for i in range(3)]
            + [f"agent/src/h{i}.ts" for i in range(3)]
        )
        s, _ = _score(files, direction=direction)
        assert s < 0.45, f"extreme case failed to escalate: score={s:.3f}"

    def test_single_extreme_dimension_escalates(self):
        """A single extreme dimension (e.g. very high expansion ratio)
        should drive the composite below the threshold on its own when
        combined with any other mild penalty."""
        direction = {
            "research_history": [
                {"scope_breakdown": {"medium": {"avg_expansion_ratio": 20.0}}},
            ]
        }
        files = [f"backend/app/f{i}.py" for i in range(8)]
        s, _ = _score(files, direction=direction)
        assert s < 0.45


class TestClampingAndEmpty:
    def test_score_bounded(self):
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

    def test_empty_related_files(self):
        s, meta = _score([], direction=None, cochange_index=None)
        assert s == 1.0
        assert meta["files"] == 0
        assert meta["expansion_ratio"] == 0.0
        assert meta["modules"] == 0

    def test_none_direction_and_cochange(self):
        s, meta = _score(
            ["pkg/sub/a.py", "pkg/sub/b.py", "pkg/sub/c.py"],
            direction=None,
            cochange_index=None,
        )
        assert abs(s - (1.0 - 0.0625)) < 1e-9
        assert meta["expansion_ratio"] == 0.0

    def test_alembic_migrations_excluded_from_factors(self):
        s, meta = _score(
            ["backend/app/models.py", "backend/alembic/versions/0037_x.py"],
        )
        assert s == 1.0
        assert meta["files"] == 1
        assert meta["modules"] == 1
        assert meta["auto_generated_files"] == 1

    def test_none_related_files_treated_as_empty(self):
        s, meta = _composite_complexity_score(None, None, None)  # type: ignore[arg-type]
        assert s == 1.0
        assert meta["files"] == 0
