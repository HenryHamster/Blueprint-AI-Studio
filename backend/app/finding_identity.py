"""Shared deterministic identity for durable audit/review findings."""

from __future__ import annotations

import hashlib


def audit_finding_key(summary: str, file: str | None) -> str:
    """Return the canonical content key for a finding.

    The key deliberately remains the existing normalized ``file|summary``
    sha256 prefix so release qualification and review-chain consumers share
    one identity convention.
    """
    norm_file = str(file or "").strip().lower()
    norm_summary = str(summary or "").strip().lower()
    return hashlib.sha256(f"{norm_file}|{norm_summary}".encode()).hexdigest()[:16]


_audit_finding_key = audit_finding_key
