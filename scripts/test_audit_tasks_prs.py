import audit_tasks_prs


def test_visual_pilot_report_aggregates_and_lists_catches(capsys):
    tasks = [
        {
            "id": "task-one-abcdef",
            "status": "COMPLETED",
            "created_at": "2026-06-01T00:00:00Z",
            "pr_url": "https://github.com/example/repo/pull/12",
            "context": {
                "_pr_changed_files": ["admin-ui/src/App.tsx"],
                "_self_review_outcome": {
                    "fixes_applied": True,
                    "visual_verification": {
                        "pairs_inspected": 2,
                        "pairs_changed": 1,
                        "caught_visual_issue": True,
                    },
                },
            },
        },
        {
            "id": "task-two-abcdef",
            "status": "COMPLETED",
            "created_at": "2026-06-02T00:00:00Z",
            "context": {
                "_pr_changed_files": ["admin-ui/src/Button.tsx"],
                "_self_review_outcome": {
                    "visual_verification": {
                        "pairs_inspected": 1,
                        "pairs_changed": 0,
                        "caught_visual_issue": False,
                        "failure_class": "VIEWPORT_GATE_INFRA_UNAVAILABLE",
                    },
                },
            },
        },
        {
            "id": "task-three-abcdef",
            "status": "COMPLETED",
            "created_at": "2026-06-03T00:00:00Z",
            "context": {"_pr_changed_files": ["backend/app/main.py"]},
        },
    ]

    audit_tasks_prs._print_visual_pilot_report(
        tasks,
        created_after="2026-06-01T00:00:00Z",
        created_before=None,
    )

    out = capsys.readouterr().out
    assert "Completed admin-ui-touching tasks scanned: 2" in out
    assert "Tasks carrying visual_verification entries: 2" in out
    assert "Pairs inspected/changed: 3 / 1" in out
    assert "Infra-failure occurrences (viewport infra-unavailable): 1" in out
    assert "task-one https://github.com/example/repo/pull/12 fix_followed=True" in out
    assert "Window progress: 2 of 25 PR/tasks" in out
    assert "Catches beyond assertion gates: 1" in out
    assert "Infra-failure rate: 1/2 (50.0%)" in out


def test_visual_pilot_report_handles_absent_entries(capsys):
    audit_tasks_prs._print_visual_pilot_report(
        [
            {
                "id": "task-one-abcdef",
                "status": "COMPLETED",
                "created_at": "2026-06-01T00:00:00Z",
                "context": {"_pr_changed_files": ["admin-ui/src/App.tsx"]},
            }
        ],
        created_after="2026-06-01T00:00:00Z",
        created_before=None,
    )

    out = capsys.readouterr().out
    assert "Tasks carrying visual_verification entries: 0" in out
    assert "Pairs inspected/changed: 0 / 0" in out
    assert "- none" in out
    assert "Infra-failure rate: 0/0 (0.0%)" in out


def test_changed_files_falls_back_to_context_file_change_details():
    task = {
        "context": {
            "_file_change_details": [
                {"path": "admin-ui/src/App.tsx"},
                {"path": "backend/app/main.py"},
            ]
        }
    }

    assert audit_tasks_prs._changed_files(task) == [
        "admin-ui/src/App.tsx",
        "backend/app/main.py",
    ]


def test_main_visual_pilot_fetches_full_window_when_created_before_is_set(monkeypatch):
    calls = []

    def fake_fetch_tasks(status, limit=200, *, created_after=None, max_rows=None):
        calls.append(
            {
                "status": status,
                "limit": limit,
                "created_after": created_after,
                "max_rows": max_rows,
            }
        )
        return []

    monkeypatch.setattr(audit_tasks_prs, "_fetch_tasks", fake_fetch_tasks)
    monkeypatch.setattr(audit_tasks_prs, "_print_visual_pilot_report", lambda *args, **kwargs: None)
    monkeypatch.setattr(audit_tasks_prs, "load_config", lambda: {"default_repo": "example/repo", "project_id": "proj"})
    monkeypatch.setattr(audit_tasks_prs, "get_project_id", lambda project_id: project_id)

    audit_tasks_prs.main(
        [
            "--visual-pilot",
            "--created-after",
            "2026-06-01T00:00:00Z",
            "--created-before",
            "2026-06-15T00:00:00Z",
            "--task-window",
            "17",
        ]
    )

    assert calls == [
        {
            "status": "COMPLETED",
            "limit": 200,
            "created_after": "2026-06-01T00:00:00Z",
            "max_rows": None,
        }
    ]
