"""The topic/findings responsiveness bar must not scale with prompt length.

Both completion lanes run a cheap keyword-overlap probe to catch output about a
*different* subject than the one requested. Both expressed the bar as a bare
fraction of the request, which inverted the incentive: a 10-keyword request
needed 1 matching keyword, a 159-keyword request needed 16. The more care an
operator put into specifying a task, the harder its output had to work to be
judged on-topic.

On 2026-08-23 that killed a valid perf task at 8/159 matched whose findings were
plainly about the requested target. These tests pin the capped bar in both lanes
and keep the two lanes in step.
"""

import pytest

from app.knowledge_completion import (
    _MAX_REQUIRED_TOPIC_OVERLAP,
    _MIN_REQUEST_KEYWORDS,
    _MIN_TOPIC_OVERLAP_RATIO,
    _acceptance_probe_detail,
    _required_topic_overlap,
    _topic_responsiveness_miss,
)
from app.task_completion import (
    _FINDINGS_MAX_REQUIRED_OVERLAP,
    _FINDINGS_MIN_KWS,
    _FINDINGS_MIN_OVERLAP,
    _findings_probe_detail,
    _findings_required_overlap,
    _findings_responsiveness_miss,
)
from testing.factories import make_task


def _kws(count: int, prefix: str) -> list[str]:
    """``count`` distinct synthetic keywords the probe extractors will all keep."""
    return [f"{prefix}word{i:04d}" for i in range(count)]


# ── the bar itself ───────────────────────────────────────────────────────────


def test_absolute_cap_is_pinned_in_both_lanes():
    """Literal pins. Raising either cap loosens a gate, so it must be deliberate.

    Deliberately bare numbers rather than a comparison against the constants
    themselves: a self-certifying assertion would move with any future edit and
    prove nothing on review.
    """
    assert _FINDINGS_MAX_REQUIRED_OVERLAP == 4
    assert _MAX_REQUIRED_TOPIC_OVERLAP == 4
    assert _FINDINGS_MIN_OVERLAP == 0.1
    assert _MIN_TOPIC_OVERLAP_RATIO == 0.1
    assert _FINDINGS_MIN_KWS == 3
    assert _MIN_REQUEST_KEYWORDS == 3


def test_the_two_lanes_share_one_bar():
    """The findings and knowledge probes answer the same question; they must agree."""
    assert _FINDINGS_MAX_REQUIRED_OVERLAP == _MAX_REQUIRED_TOPIC_OVERLAP
    for count in (3, 5, 10, 20, 40, 41, 100, 159, 1000):
        assert _findings_required_overlap(count) == _required_topic_overlap(count)


@pytest.mark.parametrize(
    ("request_keywords", "expected"),
    [
        # Below the cap the bar is unchanged from the old bare fraction, so this
        # is a pure relaxation: short and moderately detailed requests are graded
        # exactly as before.
        (3, 1),
        (10, 1),
        (11, 2),
        (20, 2),
        (40, 4),
        # ...and from here the old fraction kept climbing (41 -> 5, 159 -> 16)
        # while the real question — "is this about a different subject?" — does
        # not get harder to answer as the prompt gets longer.
        (41, 4),
        (100, 4),
        (159, 4),
        (5000, 4),
    ],
)
def test_required_overlap_is_the_fraction_capped(request_keywords, expected):
    assert _findings_required_overlap(request_keywords) == expected
    assert _required_topic_overlap(request_keywords) == expected


def test_a_request_that_clears_the_signal_floor_still_needs_one_match():
    """The floor is one, never zero — output sharing nothing at all is still a miss."""
    assert _findings_required_overlap(3) == 1
    assert _required_topic_overlap(3) == 1


# ── findings lane ────────────────────────────────────────────────────────────


def _findings_task(request_keyword_count: int):
    return make_task(
        payloads=[{"prompt": " ".join(_kws(request_keyword_count, "req"))}]
    )


def _findings_with(shared: list[str], noise: int = 0):
    return [
        {
            "title": " ".join(shared),
            "description": " ".join(_kws(noise, "other")),
            "file_path": "backend/app/thing.py",
        }
    ]


def test_detailed_perf_request_is_no_longer_killed_at_8_of_159():
    """The exact 2026-08-23 incident: a valid task lost to its own prompt length."""
    task = _findings_task(159)
    findings = _findings_with(_kws(8, "req"))
    assert _findings_responsiveness_miss(task, findings) is None


def test_findings_about_a_different_subject_are_still_a_miss():
    task = _findings_task(159)
    findings = _findings_with([], noise=30)
    miss = _findings_responsiveness_miss(task, findings)
    assert miss is not None
    assert "0/159" in miss


def test_three_shared_keywords_is_still_short_of_the_bar():
    """The cap is 4, so 3 must fail — proves the bound is enforced, not decorative."""
    task = _findings_task(159)
    assert _findings_responsiveness_miss(task, _findings_with(_kws(3, "req"))) is not None
    assert _findings_responsiveness_miss(task, _findings_with(_kws(4, "req"))) is None


def test_a_short_request_is_graded_exactly_as_before():
    """No regression for the common case: 10 keywords still needs only 1 match."""
    task = _findings_task(10)
    assert _findings_responsiveness_miss(task, _findings_with(_kws(1, "req"))) is None
    assert _findings_responsiveness_miss(task, _findings_with([], noise=5)) is not None


def test_findings_miss_reason_names_the_bar_it_failed():
    """Surface reasons, not states: an operator must see what was required."""
    task = _findings_task(159)
    miss = _findings_responsiveness_miss(task, _findings_with(_kws(2, "req")))
    assert miss is not None
    assert "2/159" in miss
    assert "4 required" in miss


def test_findings_probe_detail_carries_the_bar():
    task = _findings_task(159)
    detail = _findings_probe_detail(task, _findings_with(_kws(8, "req")))
    probe = detail["topic_responsiveness"]
    assert probe["overlap"] == 8
    assert probe["total"] == 159
    assert probe["required"] == 4
    assert probe["passed"] is True


# ── knowledge lane ───────────────────────────────────────────────────────────


def _knowledge_task(request_keyword_count: int):
    return make_task(
        payloads=[
            {
                "type": "web_research",
                "topic": " ".join(_kws(request_keyword_count, "req")),
            }
        ]
    )


def _document(shared: list[str], noise: int = 0):
    return {
        "title": " ".join(shared),
        "summary": " ".join(_kws(noise, "other")),
        "body": "",
    }


def test_detailed_topic_no_longer_fails_a_responsive_document():
    task = _knowledge_task(159)
    assert _topic_responsiveness_miss(task, _document(_kws(8, "req"))) is None


def test_document_about_a_different_topic_is_still_a_miss():
    task = _knowledge_task(159)
    miss = _topic_responsiveness_miss(task, _document([], noise=30))
    assert miss is not None
    assert "0/159" in miss


def test_knowledge_miss_reason_names_the_bar_it_failed():
    task = _knowledge_task(159)
    miss = _topic_responsiveness_miss(task, _document(_kws(2, "req")))
    assert miss is not None
    assert "2/159" in miss
    assert "4 required" in miss


def test_knowledge_cap_is_enforced_at_its_boundary():
    task = _knowledge_task(159)
    assert _topic_responsiveness_miss(task, _document(_kws(3, "req"))) is not None
    assert _topic_responsiveness_miss(task, _document(_kws(4, "req"))) is None


def test_short_topic_is_graded_exactly_as_before():
    task = _knowledge_task(10)
    assert _topic_responsiveness_miss(task, _document(_kws(1, "req"))) is None
    assert _topic_responsiveness_miss(task, _document([], noise=5)) is not None


def test_acceptance_probe_detail_carries_the_bar():
    task = _knowledge_task(159)
    detail = _acceptance_probe_detail(
        task, _document(_kws(8, "req")), kind="web_research"
    )
    probe = detail["topic_responsiveness"]
    assert probe["overlap"] == 8
    assert probe["total"] == 159
    assert probe["required"] == 4
    assert probe["passed"] is True


def test_probe_detail_reports_no_bar_when_the_request_carries_no_signal():
    """An unjudgeable request accepts; the detail must not imply a bar was applied."""
    task = make_task(payloads=[{"type": "web_research", "topic": ""}])
    detail = _acceptance_probe_detail(task, _document([]), kind="web_research")
    probe = detail["topic_responsiveness"]
    assert probe["total"] == 0
    assert probe["required"] == 0
    assert probe["passed"] is True
