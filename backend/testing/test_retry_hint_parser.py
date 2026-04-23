"""Unit tests for providers.base.parse_retry_hint_seconds.

Inputs are verbatim fragments observed in OpenAI / Gemini / Codex CLI 429
bodies. Clock-time forms ("resets on 2:49 PM", "try again at Apr 17th") are
intentionally unsupported — parse_retry_hint_seconds returns None for those
because we don't trust the client clock enough to convert them to a delta.
"""
from __future__ import annotations

import pytest

from app.providers.base import parse_retry_hint_seconds


@pytest.mark.parametrize(
    "text,expected_seconds",
    [
        # Gemini verbatims
        ("Please retry in 39.844676573s", 39),
        ("Please retry in 15s", 15),
        ("please retry in 55 seconds", 55),
        # OpenAI verbatims
        ("please try again after 60.616292 seconds", 60),
        ("try again in 1.574s", 1),
        ("please try again in 30 seconds", 30),
        ("Retry after 45 seconds", 45),
        # Minutes / hours
        ("please retry in 2 minutes", 120),
        ("try again in 1 hour", 3600),
    ],
)
def test_parse_retry_hint_seconds_verbatim(text, expected_seconds):
    assert parse_retry_hint_seconds(text) == expected_seconds


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Resets on 2:49 PM",  # clock time: unsupported
        "try again at Apr 17th, 2026 6:10 AM",  # absolute date: unsupported
        "retry in 30",  # unit required, bare number rejected
        "please retry",  # no numeric portion
        "no rate limit here",
        "server returned 429",  # no retry phrasing
    ],
)
def test_parse_retry_hint_seconds_no_match(text):
    assert parse_retry_hint_seconds(text) is None


def test_parse_retry_hint_seconds_ceiling():
    # 99999 seconds is ~27.7 hours; ceiling is 4h = 14400s.
    assert parse_retry_hint_seconds("please retry in 99999 seconds") == 14400


def test_parse_retry_hint_seconds_ignores_non_positive():
    assert parse_retry_hint_seconds("retry in 0 seconds") is None


def test_parse_retry_hint_seconds_takes_first_match():
    # First retry hint wins — later mentions are ignored.
    text = "please retry in 30s (then maybe try again in 120 seconds)"
    assert parse_retry_hint_seconds(text) == 30
