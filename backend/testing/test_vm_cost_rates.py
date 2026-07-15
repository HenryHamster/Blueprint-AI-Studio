"""Tests for VM pricing lookup and fallback behavior."""

import logging

from _pytest.logging import LogCaptureFixture

from app.vm_cost_rates import get_vm_rate_per_second


def test_non_default_machine_type_uses_its_own_rate():
    on_demand = get_vm_rate_per_second(machine_type="e2-standard-4", spot=False)
    spot = get_vm_rate_per_second(machine_type="e2-standard-4", spot=True)

    assert on_demand == 0.134 / 3600
    assert spot == 0.04 / 3600


def test_c2d_capacity_fallback_uses_explicit_us_central1_rate():
    on_demand = get_vm_rate_per_second(machine_type="c2d-standard-2", spot=False)
    spot = get_vm_rate_per_second(machine_type="c2d-standard-2", spot=True)

    assert on_demand == 0.090798 / 3600
    assert spot == 0.023988 / 3600


def test_unknown_machine_type_logs_and_falls_back(caplog: LogCaptureFixture):
    with caplog.at_level(logging.WARNING):
        on_demand = get_vm_rate_per_second(machine_type="e2-unknown-999", spot=False)
        spot = get_vm_rate_per_second(machine_type="e2-unknown-999", spot=True)

    assert on_demand == 0.067 / 3600
    assert spot == 0.020 / 3600
    assert "falling back to e2-standard-2 pricing" in caplog.text
