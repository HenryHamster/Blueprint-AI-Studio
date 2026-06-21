"""Tests for compute_cloud_run helpers."""

import sys
import types
import pytest


def test_build_duration_raises_on_missing_protobuf(monkeypatch):
    """_build_duration must raise ImportError when google.protobuf is absent."""
    # Remove google.protobuf from sys.modules so the lazy import fails
    saved = {}
    for key in list(sys.modules):
        if key == "google.protobuf" or key.startswith("google.protobuf."):
            saved[key] = sys.modules.pop(key)

    # Also stub google so the import path resolves up to the point of the sub-import
    google_stub = types.ModuleType("google")
    monkeypatch.setitem(sys.modules, "google", google_stub)
    monkeypatch.setitem(sys.modules, "google.protobuf", None)  # type: ignore[arg-type]

    try:
        from app.compute_cloud_run import _build_duration  # noqa: PLC0415

        with pytest.raises(ImportError, match="google.protobuf"):
            _build_duration(300)
    finally:
        # Restore saved modules
        for key, mod in saved.items():
            sys.modules[key] = mod
