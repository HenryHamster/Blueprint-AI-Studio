"""Coverage retained while removing scheduler-owned qualification paths."""


def test_is_ci_infra_flake_log_returns_false_for_real_failure():
    from app.failure_classification import _is_ci_infra_flake_log

    assert not _is_ci_infra_flake_log(
        "FAILED testing/test_foo.py::test_bar\nAssertionError: assert 0 == 1"
    )
    assert not _is_ci_infra_flake_log("")
    assert not _is_ci_infra_flake_log("SyntaxError: invalid syntax at line 42")
