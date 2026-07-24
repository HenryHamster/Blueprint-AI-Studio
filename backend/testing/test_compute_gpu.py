"""
Tests for GPU detection, pricing facts, and image-family validation.

Covers:
- detect_local_gpu (absent / present / subprocess error)
- validate_backend_name for the local-gpu backend
- GPU pricing rates (N1+T4, N1+V100, G2+L4) and the accelerator gate
- gpu_zone_candidates (non-empty, subnet-correct, distinct from fleet ring)
- _ensure_gpu_image_family_valid gate
"""

import pytest

from app import compute
def test_detect_local_gpu_absent(monkeypatch):
    """detect_local_gpu returns None when nvidia-smi is not on the PATH."""
    from app import compute_backends as cb

    monkeypatch.setattr(cb.shutil, "which", lambda _name: None)
    result = cb.detect_local_gpu()
    assert result is None


def test_detect_local_gpu_present(monkeypatch):
    """detect_local_gpu returns parsed name + memory when nvidia-smi succeeds."""
    from app import compute_backends as cb

    monkeypatch.setattr(cb.shutil, "which", lambda _name: "/usr/bin/nvidia-smi")

    class _FakeResult:
        returncode = 0
        stdout = "Tesla T4, 15360\n"

    monkeypatch.setattr(cb.subprocess, "run", lambda *_a, **_kw: _FakeResult())
    result = cb.detect_local_gpu()
    assert result == {"name": "Tesla T4", "memory_mib": 15360}


def test_detect_local_gpu_subprocess_error(monkeypatch):
    """detect_local_gpu returns None and does not raise when nvidia-smi errors."""
    from app import compute_backends as cb

    monkeypatch.setattr(cb.shutil, "which", lambda _name: "/usr/bin/nvidia-smi")

    def _raise(*_a, **_kw):
        raise RuntimeError("oops")

    monkeypatch.setattr(cb.subprocess, "run", _raise)
    result = cb.detect_local_gpu()
    assert result is None


def test_validate_backend_name_local_gpu_no_gpu(monkeypatch):
    """validate_backend_name raises for local-gpu when no GPU is detected."""
    from app import compute_backends as cb

    monkeypatch.setattr(cb, "detect_local_gpu", lambda: None)
    with pytest.raises(ValueError, match="no local GPU detected"):
        cb.validate_backend_name("local-gpu")


def test_validate_backend_name_local_gpu_present(monkeypatch):
    """validate_backend_name accepts local-gpu when a GPU is detected."""
    from app import compute_backends as cb

    monkeypatch.setattr(cb, "detect_local_gpu", lambda: {"name": "T4", "memory_mib": 15360})
    assert cb.validate_backend_name("local-gpu") == "local-gpu"



# ---------------------------------------------------------------------------
# Single-GPU FACTS — pricing, accelerator gate, and zone candidates (inert).
# ---------------------------------------------------------------------------


def test_gpu_rate_includes_base_plus_accelerator():
    """N1 GPU rate = base machine + GPU surcharge (on-demand and spot)."""
    from app import vm_cost_rates as vcr

    # N1 + T4: base n1-standard-1 + T4 surcharge.
    t4_on = vcr.get_compute_rate_per_second("n1-standard-1", accelerator="T4")
    t4_spot = vcr.get_compute_rate_per_second(
        "n1-standard-1", accelerator="T4", spot=True
    )
    assert t4_on == pytest.approx(0.39749975 / 3600)
    assert t4_spot == pytest.approx(0.17481125 / 3600)
    # Explicitly = base + surcharge, not a bundled lookup.
    assert t4_on == pytest.approx((0.04749975 + 0.35) / 3600)
    assert t4_spot == pytest.approx((0.01381125 + 0.161) / 3600)

    # N1 + V100.
    v100_on = vcr.get_compute_rate_per_second("n1-standard-1", accelerator="V100")
    v100_spot = vcr.get_compute_rate_per_second(
        "n1-standard-1", accelerator="V100", spot=True
    )
    assert v100_on == pytest.approx(2.52749975 / 3600)
    assert v100_spot == pytest.approx(1.10501125 / 3600)
    assert v100_on == pytest.approx((0.04749975 + 2.48) / 3600)


def test_gpu_rate_g2_l4_is_bundled():
    """G2 + L4 rate comes from the bundled machine price (no separate surcharge)."""
    from app import vm_cost_rates as vcr

    l4_on = vcr.get_compute_rate_per_second("g2-standard-4", accelerator="L4")
    l4_spot = vcr.get_compute_rate_per_second(
        "g2-standard-4", accelerator="L4", spot=True
    )
    assert l4_on == pytest.approx(0.70683228 / 3600)
    assert l4_spot == pytest.approx(0.058736 / 3600)
    # L4 has no entry in the per-GPU surcharge table — it is bundled into G2.
    assert "nvidia-l4" not in vcr.GPU_ACCELERATOR_SURCHARGE_RATES


def test_compute_rate_without_accelerator_matches_cpu_rate():
    """accelerator=None falls through to the unchanged CPU (e2) accessor."""
    from app import vm_cost_rates as vcr

    for mt in vcr.supported_machine_types():
        assert vcr.get_compute_rate_per_second(mt) == vcr.get_vm_rate_per_second(mt)
        assert vcr.get_compute_rate_per_second(
            mt, spot=True
        ) == vcr.get_vm_rate_per_second(mt, spot=True)


def test_accelerator_gate_rejects_a100_count_and_unknown():
    """A100 (out of quota), count!=1, and unknown names are rejected."""
    from app import vm_cost_rates as vcr

    spec, reason = vcr.validate_accelerator_request("A100")
    assert spec is None and reason and "A100" in reason

    spec, reason = vcr.validate_accelerator_request("T4", count=2)
    assert spec is None and reason and "count" in reason

    spec, reason = vcr.validate_accelerator_request("nvidia-tesla-h100")
    assert spec is None and reason

    # The accessor surfaces the same rejections as exceptions.
    with pytest.raises(vcr.AcceleratorRejection):
        vcr.get_compute_rate_per_second("a2-highgpu-1g", accelerator="A100")
    with pytest.raises(vcr.AcceleratorRejection):
        vcr.get_compute_rate_per_second("n1-standard-1", accelerator="T4", count=4)


def test_supported_accelerators_type_strings_and_families():
    """Each accelerator maps to the correct GCP type string and machine family."""
    from app import vm_cost_rates as vcr

    assert set(vcr.SUPPORTED_ACCELERATORS) == {"T4", "L4", "V100"}
    assert "A100" not in vcr.SUPPORTED_ACCELERATORS

    t4 = vcr.SUPPORTED_ACCELERATORS["T4"]
    assert t4.gcp_type == "nvidia-tesla-t4"
    assert t4.machine_family == "n1" and not t4.bundled
    assert t4.machine_type == "n1-standard-1"

    v100 = vcr.SUPPORTED_ACCELERATORS["V100"]
    assert v100.gcp_type == "nvidia-tesla-v100"
    assert v100.machine_family == "n1" and not v100.bundled

    l4 = vcr.SUPPORTED_ACCELERATORS["L4"]
    assert l4.gcp_type == "nvidia-l4"
    assert l4.machine_family == "g2" and l4.bundled
    assert l4.machine_type == "g2-standard-4"


def test_gpu_facts_do_not_leak_into_cpu_create_gate():
    """Regression guard: GPU shapes must NOT enter the CPU-only create gate."""
    from app import vm_cost_rates as vcr

    assert set(vcr.supported_machine_types()) == {
        "c2d-standard-2",
        "n2-standard-2",
        "e2-standard-2",
        "e2-standard-4",
        "e2-standard-8",
        "e2-standard-16",
        "e2-standard-32",
    }
    for gpu_mt in ("n1-standard-1", "g2-standard-4"):
        assert not vcr.is_supported_machine_type(gpu_mt)
        assert gpu_mt not in vcr.VM_COST_RATES


def test_gpu_zone_candidates_non_empty_mapped_and_distinct_from_fleet():
    """Per-accelerator zone candidates are non-empty, subnet-correct, and not the fleet ring."""
    from app import compute

    fleet_zones = {z for z, _ in compute.FALLBACK_ZONES}

    region_subnets = {}
    for z, s in compute.FALLBACK_ZONES:
        region_subnets.setdefault(z.rsplit("-", 1)[0], s)

    for accel in ("T4", "L4", "V100", "nvidia-tesla-t4"):
        candidates = compute.gpu_zone_candidates(accel)
        assert candidates, f"expected non-empty zone list for {accel}"
        for zone, subnet in candidates:
            assert subnet == region_subnets[zone.rsplit("-", 1)[0]]

    # V100 has no us-east4 zones; T4 does — the maps are genuinely per-accelerator.
    t4_zones = {z for z, _ in compute.gpu_zone_candidates("T4")}
    v100_zones = {z for z, _ in compute.gpu_zone_candidates("V100")}
    assert any(z.startswith("us-east4") for z in t4_zones)
    assert not any(z.startswith("us-east4") for z in v100_zones)

    # Independent of the fleet ring: the GPU lists include zones not in FALLBACK_ZONES.
    assert t4_zones - fleet_zones, "GPU zone ring should differ from the fleet ring"

    # Unknown accelerators yield an empty candidate list.
    assert compute.gpu_zone_candidates("nvidia-a100") == []


def test_gpu_image_family_is_driver_preinstalled():
    """GPU image family defaults to a driver-preinstalled DLVM family."""
    from app import compute

    # Driver-preinstalled Deep Learning VM family (NVIDIA driver baked in).
    assert "nvidia" in compute.GPU_IMAGE_FAMILY
    assert "deeplearning-platform-release" in compute.GPU_IMAGE_FAMILY
    # Pinned the same way AGENT_IMAGE_FAMILY is (fully-qualified family path).
    assert compute.GPU_IMAGE_FAMILY.startswith("projects/")


class TestGpuImageFamilyValidation:
    """GPU image family is validated at startup and gates GPU provisioning."""

    def test_ensure_gpu_image_family_valid_raises_when_invalid(self, monkeypatch):
        """_ensure_gpu_image_family_valid raises RuntimeError when GPU image is invalid."""
        monkeypatch.setattr(compute, "_gpu_image_family_valid", False)
        monkeypatch.setattr(compute, "_gpu_image_family_last_check", 1.0)
        monkeypatch.setattr(compute, "LOCAL_MODE", False)
        # Set last check to far in the future so recheck interval has NOT elapsed.
        import time

        monkeypatch.setattr(compute, "_gpu_image_family_last_check", time.time())

        with pytest.raises(RuntimeError, match="GPU"):
            compute._ensure_gpu_image_family_valid()

    def test_ensure_gpu_image_family_valid_passes_when_valid(self, monkeypatch):
        """_ensure_gpu_image_family_valid is a no-op when GPU image is valid."""
        monkeypatch.setattr(compute, "_gpu_image_family_valid", True)
        monkeypatch.setattr(compute, "LOCAL_MODE", False)
        # Should not raise.
        compute._ensure_gpu_image_family_valid()

    def test_ensure_gpu_image_family_valid_rechecks_after_interval(self, monkeypatch):
        """Re-validates after _IMAGE_FAMILY_RECHECK_INTERVAL elapses."""
        monkeypatch.setattr(compute, "_gpu_image_family_valid", False)
        monkeypatch.setattr(compute, "_gpu_image_family_last_check", 0.0)
        monkeypatch.setattr(compute, "LOCAL_MODE", False)

        validate_calls = []

        def fake_validate():
            validate_calls.append(True)
            # Simulate successful validation by letting state be set normally.

        monkeypatch.setattr(compute, "_validate_gpu_image_family", fake_validate)

        # Should not raise — recheck interval elapsed, revalidation succeeds.
        compute._ensure_gpu_image_family_valid()
        assert len(validate_calls) == 1
        assert compute._gpu_image_family_valid is True

    def test_ensure_gpu_image_family_valid_noop_in_local_mode(self, monkeypatch):
        """_ensure_gpu_image_family_valid is a no-op in LOCAL_MODE even when invalid."""
        monkeypatch.setattr(compute, "_gpu_image_family_valid", False)
        monkeypatch.setattr(compute, "LOCAL_MODE", True)
        # Should not raise in LOCAL_MODE regardless of GPU image state.
        compute._ensure_gpu_image_family_valid()

    async def test_provision_ephemeral_vm_gpu_calls_gpu_gate(self, monkeypatch):
        """provision_ephemeral_vm calls _ensure_gpu_image_family_valid when accelerator is set."""
        monkeypatch.setattr(compute, "_image_family_valid", True)
        monkeypatch.setattr(compute, "_gpu_image_family_valid", True)
        monkeypatch.setattr(compute, "LOCAL_MODE", False)

        gpu_gate_calls = []

        def fake_gpu_gate():
            gpu_gate_calls.append(True)

        monkeypatch.setattr(compute, "_ensure_gpu_image_family_valid", fake_gpu_gate)

        async def fake_provision_gcp(**kwargs):
            return True

        monkeypatch.setattr(compute, "_provision_gcp", fake_provision_gcp)

        await compute.provision_ephemeral_vm(
            "compute-job-vm-test",
            startup_script="#!/bin/bash\ntrue",
            accelerator="T4",
        )
        assert len(gpu_gate_calls) == 1

    async def test_provision_ephemeral_vm_cpu_skips_gpu_gate(self, monkeypatch):
        """provision_ephemeral_vm does NOT call _ensure_gpu_image_family_valid when accelerator is None."""
        monkeypatch.setattr(compute, "_image_family_valid", True)
        monkeypatch.setattr(compute, "LOCAL_MODE", False)

        gpu_gate_calls = []

        def fake_gpu_gate():
            gpu_gate_calls.append(True)

        monkeypatch.setattr(compute, "_ensure_gpu_image_family_valid", fake_gpu_gate)

        async def fake_provision_gcp(**kwargs):
            return True

        monkeypatch.setattr(compute, "_provision_gcp", fake_provision_gcp)

        await compute.provision_ephemeral_vm(
            "compute-job-vm-cpu-test",
            startup_script="#!/bin/bash\ntrue",
            accelerator=None,
        )
        assert len(gpu_gate_calls) == 0

    def test_gpu_validation_failure_does_not_block_cpu_provisioning(self, monkeypatch):
        """A failed GPU image validation leaves CPU provisioning (_ensure_image_family_valid) unaffected."""
        # CPU image is valid; GPU image is not.
        monkeypatch.setattr(compute, "_image_family_valid", True)
        monkeypatch.setattr(compute, "_gpu_image_family_valid", False)
        monkeypatch.setattr(compute, "LOCAL_MODE", False)
        import time

        monkeypatch.setattr(compute, "_gpu_image_family_last_check", time.time())

        # CPU gate passes.
        compute._ensure_image_family_valid()

        # GPU gate raises.
        with pytest.raises(RuntimeError, match="GPU"):
            compute._ensure_gpu_image_family_valid()


