"""Standalone unit tests for the reproduce-on-HEAD scoring leaf module.

Covers the additive context-key recorder.
Pure in-memory — runs under ``bash scripts/test-backend.sh --standalone``.
"""

import pytest

from app.reproduction_scoring import (
    TARGET_DEFECT_REPRODUCTION_KEY,
    record_target_defect_reproduction,
)
from app.source_ref_validation import (
    CITATION_STATE_ABSENT,
    CITATION_STATE_INDETERMINATE,
    CITATION_STATE_PRESENT,
)


@pytest.mark.parametrize(
    "verdict",
    [CITATION_STATE_PRESENT, CITATION_STATE_ABSENT, CITATION_STATE_INDETERMINATE],
)
def test_record_stamps_recognized_verdict(verdict):
    ctx: dict = {}
    record_target_defect_reproduction(ctx, verdict)
    assert ctx[TARGET_DEFECT_REPRODUCTION_KEY] == verdict


@pytest.mark.parametrize("verdict", [None, "", "bogus", "reproduced", 1])
def test_record_ignores_unrecognized_verdict(verdict):
    ctx: dict = {}
    record_target_defect_reproduction(ctx, verdict)
    assert TARGET_DEFECT_REPRODUCTION_KEY not in ctx


def test_record_does_not_clobber_other_keys():
    ctx = {"other": 1}
    record_target_defect_reproduction(ctx, CITATION_STATE_PRESENT)
    assert ctx == {"other": 1, TARGET_DEFECT_REPRODUCTION_KEY: CITATION_STATE_PRESENT}


