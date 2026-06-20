import audit_tasks_prs


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


