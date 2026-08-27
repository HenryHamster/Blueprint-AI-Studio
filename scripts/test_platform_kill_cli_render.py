"""Focused rendering coverage for the operator platform-kill command."""

import blueprint


def test_platform_kill_renderer_surfaces_daily_causal_classes(capsys):
    blueprint._render_analytics_platform_kills(
        {
            "window_days": 1,
            "window_start": "2026-08-26",
            "terminal_task_count": 2,
            "platform_kill_count": 2,
            "platform_kills_per_day": 2.0,
            "classified_fraction": 1.0,
            "by_class": [],
            "daily": [
                {
                    "date": "2026-08-26",
                    "platform_kills": 2,
                    "terminal_tasks": 2,
                    "platform_kills_by_class": {
                        "AGENT_TIMEOUT": 1,
                        "PROVISION_FAILURE": 1,
                    },
                }
            ],
        }
    )

    assert "[AGENT_TIMEOUT=1, PROVISION_FAILURE=1]" in capsys.readouterr().out
