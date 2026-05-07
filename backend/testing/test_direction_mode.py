"""Unit tests for direction_mode.maybe_auto_clear_mode."""

from app.direction_mode import (
    _AUTO_CLEAR_CONSECUTIVE_THRESHOLD,
    _HEALTHY_CONSECUTIVE_KEY,
    _RECOMMENDED_MODE_META_KEY,
    maybe_auto_clear_mode,
)


def _audit_direction(source: str = "self_audit") -> dict:
    return {
        "id": "d1",
        "status": "active",
        "system_id": "sys-auth",
        "recommended_mode": "audit",
        _RECOMMENDED_MODE_META_KEY: {"source": source, "set_at": "2026-04-01T00:00:00"},
    }


class TestMaybeAutoClearMode:
    def test_no_op_when_mode_is_ship(self):
        d = {**_audit_direction(), "recommended_mode": "ship"}
        d[_RECOMMENDED_MODE_META_KEY]["mode"] = "ship"
        assert maybe_auto_clear_mode(d, "ship") is False
        assert "recommended_mode" in d  # untouched

    def test_no_op_when_not_in_audit_or_fundamentals(self):
        d = {
            "recommended_mode": "pause",
            _RECOMMENDED_MODE_META_KEY: {"source": "self_audit"},
        }
        assert maybe_auto_clear_mode(d, "ship") is False

    def test_clears_after_consecutive_threshold_self_audit(self):
        d = _audit_direction(source="self_audit")
        for i in range(_AUTO_CLEAR_CONSECUTIVE_THRESHOLD - 1):
            assert maybe_auto_clear_mode(d, "ship") is False, (
                f"should not clear on tick {i + 1}"
            )
        assert maybe_auto_clear_mode(d, "ship") is True
        assert "recommended_mode" not in d
        assert d.get("_mode_auto_cleared_from") == "audit"
        assert "_mode_auto_cleared_at" in d
        assert _HEALTHY_CONSECUTIVE_KEY not in d

    def test_clears_after_consecutive_threshold_strategic(self):
        d = _audit_direction(source="strategic")
        for _ in range(_AUTO_CLEAR_CONSECUTIVE_THRESHOLD - 1):
            maybe_auto_clear_mode(d, "ship")
        assert maybe_auto_clear_mode(d, "ship") is True
        assert "recommended_mode" not in d

    def test_clears_fundamentals_mode(self):
        d = {
            "recommended_mode": "fundamentals",
            _RECOMMENDED_MODE_META_KEY: {"source": "self_audit"},
        }
        for _ in range(_AUTO_CLEAR_CONSECUTIVE_THRESHOLD - 1):
            maybe_auto_clear_mode(d, "ship")
        assert maybe_auto_clear_mode(d, "ship") is True
        assert d.get("_mode_auto_cleared_from") == "fundamentals"

    def test_does_not_clear_operator_pinned(self):
        d = _audit_direction(source="operator")
        for _ in range(_AUTO_CLEAR_CONSECUTIVE_THRESHOLD + 5):
            assert maybe_auto_clear_mode(d, "ship") is False
        assert d["recommended_mode"] == "audit"

    def test_does_not_clear_unknown_source(self):
        d = _audit_direction(source="unknown_future_source")
        for _ in range(_AUTO_CLEAR_CONSECUTIVE_THRESHOLD + 2):
            assert maybe_auto_clear_mode(d, "ship") is False

    def test_resets_count_when_maturity_not_ship(self):
        d = _audit_direction()
        maybe_auto_clear_mode(d, "ship")  # count=1
        maybe_auto_clear_mode(d, "audit")  # resets
        assert d.get(_HEALTHY_CONSECUTIVE_KEY) == 0
        # Must reach threshold again from zero
        maybe_auto_clear_mode(d, "ship")  # count=1 again
        assert maybe_auto_clear_mode(d, "ship") is True  # count=2 → clears

    def test_resets_count_when_drift_flags(self):
        d = _audit_direction()
        maybe_auto_clear_mode(d, "ship")  # count=1
        maybe_auto_clear_mode(d, "ship", has_drift_flags=True)  # resets
        assert d.get(_HEALTHY_CONSECUTIVE_KEY) == 0

    def test_no_drift_flags_default(self):
        d = _audit_direction()
        for _ in range(_AUTO_CLEAR_CONSECUTIVE_THRESHOLD - 1):
            maybe_auto_clear_mode(d, "ship")
        assert maybe_auto_clear_mode(d, "ship", has_drift_flags=False) is True

    def test_no_op_when_meta_missing(self):
        d = {"recommended_mode": "audit"}  # no meta key at all
        assert maybe_auto_clear_mode(d, "ship") is False
        assert d["recommended_mode"] == "audit"

    def test_walk_audit_set_maturity_recovers_mode_clears(self):
        """End-to-end scenario: direction put into audit, then system recovers."""
        direction = {
            "id": "d-walk",
            "status": "active",
            "system_id": "sys-payment",
            "recommended_mode": "audit",
            _RECOMMENDED_MODE_META_KEY: {
                "source": "self_audit",
                "set_at": "2026-04-01T00:00:00",
                "mode": "audit",
            },
        }

        # Tick 1: system reports ship but only one healthy assessment — not enough
        cleared = maybe_auto_clear_mode(direction, "ship", has_drift_flags=False)
        assert cleared is False
        assert direction["recommended_mode"] == "audit"
        assert direction[_HEALTHY_CONSECUTIVE_KEY] == 1

        # Tick 2: system still reports ship, threshold reached — mode clears
        cleared = maybe_auto_clear_mode(direction, "ship", has_drift_flags=False)
        assert cleared is True
        assert "recommended_mode" not in direction
        assert direction["_mode_auto_cleared_from"] == "audit"
        assert "_mode_auto_cleared_at" in direction
