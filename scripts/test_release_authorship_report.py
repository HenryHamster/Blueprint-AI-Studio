"""Focused tests for the standalone, local-Git release authorship instrument."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import release_authorship_report as report


def _run(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _commit(repo: Path, message: str, name: str, email: str, body: str = "") -> str:
    path = repo / ("change-" + message.lower().replace(" ", "-") + ".txt")
    path.write_text(message + "\n", encoding="utf-8")
    _run(repo, "add", path.name)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }
    args = ["commit", "-m", message]
    if body:
        args.extend(["-m", body])
    _run(repo, *args, env=env)
    return _run(repo, "rev-parse", "HEAD")


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q")
    _run(repo, "config", "user.name", "Fixture")
    _run(repo, "config", "user.email", "fixture@example.com")
    (repo / "history.txt").write_text("base\n", encoding="utf-8")
    _run(repo, "add", "history.txt")
    _run(repo, "commit", "-qm", "base")
    _run(repo, "branch", "-M", "main")
    return repo


def _release_fixture(tmp_path: Path, *, trusted_side: bool) -> tuple[Path, str]:
    repo = _repository(tmp_path)
    _run(repo, "switch", "-c", "release", "-q")
    _commit(
        repo,
        "engine commit",
        report.BOT_NAME,
        next(iter(report.BOT_EMAILS)),
    )
    if trusted_side:
        _commit(
            repo,
            "human commit",
            "Human Author",
            "human@example.com",
            "Co-authored-by: Claude Opus 5 <noreply@anthropic.com>",
        )
    else:
        _commit(repo, "untrusted human commit", "Human Author", "human@example.com")
    _run(repo, "switch", "main", "-q")
    _commit(repo, "mainline change", "Main Author", "main@example.com")
    merge_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "GitHub",
        "GIT_AUTHOR_EMAIL": "noreply@github.com",
        "GIT_COMMITTER_NAME": "GitHub",
        "GIT_COMMITTER_EMAIL": "noreply@github.com",
    }
    _run(repo, "merge", "--no-ff", "release", "-m", "Merge pull request #1", env=merge_env)
    tag_commit = _run(repo, "rev-parse", "HEAD")
    _run(repo, "tag", "-a", "v99", "-m", "Release v99")
    return repo, tag_commit


def test_bot_trailer_and_recursive_title_only_merge_are_classified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _ = _release_fixture(tmp_path, trusted_side=True)

    measured = report.measure_report(repo, ["v99"])

    assert measured["state"] == "complete"
    row = measured["releases"][0]
    assert row["total_commits"] == 3
    assert row["non_bot_authored_commits"] == 2
    assert row["provenance"] == {
        "bot_identity": 1,
        "co_authored_by": 1,
        "merged_parent": 1,
        "unattributed": 0,
    }
    assert row["zero_unattributed_commits"] is True

    assert report.main(["--measure", "--repo", str(repo), "--version", "v99"]) == 0
    cli_result = json.loads(capsys.readouterr().out)
    assert cli_result["releases"][0]["provenance"]["merged_parent"] == 1


def test_title_only_merge_does_not_create_provenance(tmp_path: Path) -> None:
    repo, _ = _release_fixture(tmp_path, trusted_side=False)

    row = report.measure_report(repo, ["v99"])["releases"][0]

    assert row["provenance"]["merged_parent"] == 0
    assert row["provenance"]["unattributed"] == 2
    assert len(row["unattributed_shas"]) == 2
    assert row["zero_unattributed_commits"] is False


def test_malformed_and_non_ancestor_ranges_fail_closed(tmp_path: Path) -> None:
    repo, candidate = _release_fixture(tmp_path, trusted_side=True)

    with pytest.raises(report.IncompleteHistory, match="git read failed"):
        report._range_commits(repo, "not-a-sha", candidate)

    unrelated_tree = _run(repo, "rev-parse", f"{candidate}^{{tree}}")
    unrelated = _run(repo, "commit-tree", unrelated_tree, "-m", "unrelated")
    with pytest.raises(report.IncompleteHistory, match="git read failed"):
        report._range_commits(repo, unrelated, candidate)


def test_shallow_checkout_returns_incomplete_history_not_zero(tmp_path: Path) -> None:
    repo, _ = _release_fixture(tmp_path, trusted_side=True)
    shallow = tmp_path / "shallow"
    _run(tmp_path, "clone", "--depth", "1", "--branch", "v99", f"file://{repo}", str(shallow))

    result = report.measure_report(shallow, ["v99"])

    assert result["state"] == "incomplete_history"
    assert "shallow" in result["reason"]
    assert result["releases"] == []


def test_committed_contract_is_the_only_render_source() -> None:
    root = Path(__file__).resolve().parents[1]
    contract_path = root / "scripts" / "release_authorship_report_contract.json"
    document_path = root / "docs" / "findings" / "release-authorship-history.md"

    contract = report.load_contract(contract_path)
    rendered = report.render_contract(contract)

    assert [row["version"] for row in contract["releases"]] == ["v37", "v38", "v42", "v45"]
    assert [row["total_commits"] for row in contract["releases"]] == [3, 18, 15, 149]
    assert all(row["zero_unattributed_commits"] for row in contract["releases"])
    assert all(row["force_promote_history"]["status"] == "unrecoverable" for row in contract["releases"])
    assert "last_qualification" in document_path.read_text(encoding="utf-8")
    assert document_path.read_text(encoding="utf-8") == rendered
    assert json.loads(contract_path.read_text(encoding="utf-8"))["contract_sha256"] == report.contract_sha256(contract)


def test_verify_contract_rejects_matching_unattributed_measurement(tmp_path: Path) -> None:
    repo, _ = _release_fixture(tmp_path, trusted_side=False)
    measured = report.measure_report(repo, ["v99"])
    assert measured["state"] == "complete"

    row = measured["releases"][0]
    row["force_promote_history"] = {
        "status": "unrecoverable",
        "reason": "last_qualification did not retain historical release-mechanic evidence",
    }
    contract = {"schema_version": report.CONTRACT_SCHEMA_VERSION, "releases": [row]}
    contract["contract_sha256"] = report.contract_sha256(contract)
    contract_path = tmp_path / "contract.json"
    document_path = tmp_path / "report.md"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    document_path.write_text(report.render_contract(contract), encoding="utf-8")

    ok, message = report.verify_contract(repo, contract_path, document_path)

    assert not ok
    assert "unattributed" in message
