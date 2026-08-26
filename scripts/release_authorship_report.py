#!/usr/bin/env python3
"""Measure release authorship from immutable local Git history.

This instrument intentionally has no Blueprint, database, GitHub, or network
dependency.  A release range is taken from an annotated release tag's peeled
merge commit: its first parent is the base and the tagged commit is the
candidate.  A non-Bot commit is engine-provenanced only when it has an
accepted ``Co-authored-by`` trailer or is a merge whose merged-parent history
is recursively attributable.  GitHub's ``web-flow`` committer is never a
trust signal.

The checked-in JSON contract is the official reading.  ``--measure`` prints a
fresh local reading; it never writes or silently repairs that contract.
``--verify`` compares every hash, count, provenance bucket, and rendered
document against it.  Shallow or incomplete repositories return an explicit
``incomplete_history`` result and a non-zero exit code.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

BOT_NAME = "blueprintagentorchestrator[bot]"
BOT_EMAILS = frozenset(
    {
        "blueprintagentorchestrator[bot]@users.noreply.github.com",
        "272093198+blueprintagentorchestrator[bot]@users.noreply.github.com",
    }
)
ACCEPTED_COAUTHORS = frozenset(
    {
        ("blueprintagentorchestrator[bot]", "blueprintagentorchestrator[bot]@users.noreply.github.com"),
        ("blueprintagentorchestrator[bot]", "272093198+blueprintagentorchestrator[bot]@users.noreply.github.com"),
        ("blueprint agent", "blueprint-agent@automated.dev"),
        ("claude opus 5", "noreply@anthropic.com"),
    }
)
CONTRACT_SCHEMA_VERSION = 1
_HEX_SHA = re.compile(r"^[0-9a-f]{40}$")
_TAG_NAME = re.compile(r"^v[0-9]+$")
_COAUTHOR = re.compile(
    r"^\s*co-authored-by:\s*(?P<name>[^<>]+?)\s*<(?P<email>[^<>]+)>\s*$",
    re.IGNORECASE | re.MULTILINE,
)


class IncompleteHistory(RuntimeError):
    """The checkout cannot support a complete authoritative traversal."""


class GitReadError(IncompleteHistory):
    """A local Git read failed and therefore cannot be interpreted as empty."""


@dataclasses.dataclass(frozen=True)
class Commit:
    sha: str
    parents: tuple[str, ...]
    author_name: str
    author_email: str
    body: str


@dataclasses.dataclass(frozen=True)
class ReleaseMeasurement:
    version: str
    tag_object_sha: str
    commit_sha: str
    parents: tuple[str, ...]
    base_sha: str
    total_commits: int
    non_bot_authored_commits: int
    provenance: dict[str, int]
    unattributed_shas: tuple[str, ...]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def contract_sha256(contract: dict[str, Any]) -> str:
    """Hash the contract content, excluding its self-describing hash field."""
    payload = dict(contract)
    payload.pop("contract_sha256", None)
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _git(repo_root: Path, *args: str) -> str:
    """Run a read-only Git command with an explicit safe-directory setting."""
    root = repo_root.resolve()
    command = ["git", "-c", f"safe.directory={root}", "-C", str(root), *args]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=30,
            check=False,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise GitReadError(f"git read failed for {' '.join(args)}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise GitReadError(
            f"git read failed for {' '.join(args)} (exit {result.returncode}): {detail}"
        )
    return result.stdout


def _require_complete_checkout(repo_root: Path) -> None:
    if not (repo_root / ".git").exists():
        raise IncompleteHistory(f"{repo_root} is not a Git checkout")
    try:
        shallow = _git(repo_root, "rev-parse", "--is-shallow-repository").strip()
    except GitReadError as exc:
        raise IncompleteHistory(str(exc)) from exc
    if shallow == "true":
        raise IncompleteHistory(
            "incomplete_history: shallow Git checkout cannot prove release ancestry"
        )
    if shallow != "false":
        raise IncompleteHistory(
            f"incomplete_history: Git returned unexpected shallow state {shallow!r}"
        )


def _sha(value: str, context: str) -> str:
    normalized = value.strip().lower()
    if not _HEX_SHA.fullmatch(normalized):
        raise IncompleteHistory(f"incomplete_history: malformed {context} {value!r}")
    return normalized


def _tag_evidence(repo_root: Path, version: str) -> tuple[str, Commit]:
    if not _TAG_NAME.fullmatch(version):
        raise IncompleteHistory(f"incomplete_history: unsafe release tag {version!r}")
    tag_ref = f"refs/tags/{version}"
    tag_type = _git(repo_root, "cat-file", "-t", tag_ref).strip()
    if tag_type != "tag":
        raise IncompleteHistory(
            f"incomplete_history: release tag {version} is not an annotated tag"
        )
    tag_object_sha = _sha(_git(repo_root, "rev-parse", "--verify", tag_ref), f"{version} tag")
    commit_sha = _sha(
        _git(repo_root, "rev-parse", "--verify", f"{tag_ref}^{{commit}}"),
        f"{version} peeled commit",
    )
    commit = _read_commit(repo_root, commit_sha)
    if len(commit.parents) < 2:
        raise IncompleteHistory(
            f"incomplete_history: {version} tag commit {commit_sha} has no merge parents"
        )
    # The tag object hash is returned separately so the caller cannot mistake
    # a mutable tag ref for the immutable tag evidence.
    return tag_object_sha, commit


def _read_commit(repo_root: Path, sha: str) -> Commit:
    raw = _git(
        repo_root,
        "show",
        "-s",
        "--format=%H%x00%P%x00%an%x00%ae%x00%B",
        sha,
    )
    fields = raw.split("\x00", 4)
    if len(fields) != 5:
        raise IncompleteHistory(f"incomplete_history: malformed commit object {sha}")
    resolved_sha = _sha(fields[0], f"commit {sha}")
    parents = tuple(_sha(parent, f"parent of {resolved_sha}") for parent in fields[1].split())
    return Commit(resolved_sha, parents, fields[2], fields[3], fields[4])


def _range_commits(repo_root: Path, base_sha: str, commit_sha: str) -> tuple[str, ...]:
    # The command's success is the evidence; its output is intentionally empty.
    _git(repo_root, "merge-base", "--is-ancestor", base_sha, commit_sha)
    raw = _git(repo_root, "rev-list", "--topo-order", "--reverse", f"{base_sha}..{commit_sha}")
    shas = tuple(_sha(line, "range commit") for line in raw.splitlines() if line.strip())
    if not shas or shas[-1] != commit_sha:
        raise IncompleteHistory(
            f"incomplete_history: range {base_sha}..{commit_sha} did not resolve completely"
        )
    if len(set(shas)) != len(shas):
        raise IncompleteHistory("incomplete_history: Git returned duplicate range commits")
    return shas


def _accepted_coauthor(body: str) -> bool:
    return any(
        (match.group("name").strip().lower(), match.group("email").strip().lower())
        in ACCEPTED_COAUTHORS
        for match in _COAUTHOR.finditer(body)
    )


def _direct_bot(commit: Commit) -> bool:
    return (
        commit.author_name.strip() == BOT_NAME
        and commit.author_email.strip().lower() in BOT_EMAILS
    )


def _merged_parent_attributable(
    repo_root: Path,
    commit: Commit,
    classify: Callable[[str], str],
) -> bool:
    """Attribute only the merged side histories, never the mainline parent."""
    if len(commit.parents) < 2:
        return False
    first_parent = commit.parents[0]
    for merged_parent in commit.parents[1:]:
        side_history = _git(
            repo_root,
            "rev-list",
            "--topo-order",
            merged_parent,
            "--not",
            first_parent,
        ).splitlines()
        if not side_history:
            return False
        if any(classify(_sha(sha, "merged-parent commit")) == "unattributed" for sha in side_history):
            return False
    return True


def _measure_release(repo_root: Path, version: str) -> ReleaseMeasurement:
    tag_object_sha, tagged_commit = _tag_evidence(repo_root, version)
    base_sha = tagged_commit.parents[0]
    range_shas = _range_commits(repo_root, base_sha, tagged_commit.sha)
    commits: dict[str, Commit] = {}
    statuses: dict[str, str] = {}
    resolving: set[str] = set()

    def read(sha: str) -> Commit:
        if sha not in commits:
            commits[sha] = _read_commit(repo_root, sha)
        return commits[sha]

    def classify(sha: str) -> str:
        if sha in statuses:
            return statuses[sha]
        if sha in resolving:
            raise IncompleteHistory(f"incomplete_history: provenance cycle at {sha}")
        resolving.add(sha)
        commit = read(sha)
        if _direct_bot(commit):
            status = "bot_identity"
        elif _accepted_coauthor(commit.body):
            status = "co_authored_by"
        elif _merged_parent_attributable(repo_root, commit, classify):
            status = "merged_parent"
        else:
            status = "unattributed"
        resolving.remove(sha)
        statuses[sha] = status
        return status

    provenance = {
        "bot_identity": 0,
        "co_authored_by": 0,
        "merged_parent": 0,
        "unattributed": 0,
    }
    for sha in range_shas:
        provenance[classify(sha)] += 1
    unattributed = tuple(sha for sha in range_shas if statuses[sha] == "unattributed")
    return ReleaseMeasurement(
        version=version,
        tag_object_sha=tag_object_sha,
        commit_sha=tagged_commit.sha,
        parents=tagged_commit.parents,
        base_sha=base_sha,
        total_commits=len(range_shas),
        non_bot_authored_commits=len(range_shas) - provenance["bot_identity"],
        provenance=provenance,
        unattributed_shas=unattributed,
    )


def measure_report(repo_root: Path, versions: Iterable[str]) -> dict[str, Any]:
    """Return a JSON-safe measurement or an explicit incomplete result."""
    try:
        _require_complete_checkout(repo_root)
        releases = [_measure_release(repo_root, version) for version in versions]
    except IncompleteHistory as exc:
        return {"state": "incomplete_history", "reason": str(exc), "releases": []}
    return {
        "state": "complete",
        "releases": [_measurement_dict(release) for release in releases],
    }


def _measurement_dict(release: ReleaseMeasurement) -> dict[str, Any]:
    return {
        "version": release.version,
        "tag_object_sha": release.tag_object_sha,
        "commit_sha": release.commit_sha,
        "parents": list(release.parents),
        "base_sha": release.base_sha,
        "total_commits": release.total_commits,
        "non_bot_authored_commits": release.non_bot_authored_commits,
        "provenance": dict(release.provenance),
        "unattributed_shas": list(release.unattributed_shas),
        "zero_unattributed_commits": not release.unattributed_shas,
    }


def load_contract(path: Path) -> dict[str, Any]:
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read report contract {path}: {exc}") from exc
    if not isinstance(contract, dict):
        raise ValueError("report contract must be a JSON object")
    stored_hash = contract.get("contract_sha256")
    if stored_hash != contract_sha256(contract):
        raise ValueError("report contract self-hash does not match its content")
    if contract.get("schema_version") != CONTRACT_SCHEMA_VERSION:
        raise ValueError("unsupported report contract schema")
    releases = contract.get("releases")
    if not isinstance(releases, list) or not releases:
        raise ValueError("report contract must contain release rows")
    for row in releases:
        _validate_row(row)
    return contract


def _validate_row(row: Any) -> None:
    if not isinstance(row, dict):
        raise ValueError("report contract release row must be an object")
    required = {
        "version", "tag_object_sha", "commit_sha", "parents", "base_sha",
        "total_commits", "non_bot_authored_commits", "provenance",
        "unattributed_shas", "zero_unattributed_commits", "force_promote_history",
    }
    if set(row) != required:
        raise ValueError(f"report contract row fields drifted: {sorted(set(row) ^ required)}")
    if not isinstance(row["version"], str) or not _TAG_NAME.fullmatch(row["version"]):
        raise ValueError("report contract has an invalid release version")
    for key in ("tag_object_sha", "commit_sha", "base_sha"):
        if not isinstance(row[key], str) or not _HEX_SHA.fullmatch(row[key]):
            raise ValueError(f"report contract has an invalid {key}")
    if (
        not isinstance(row["parents"], list)
        or len(row["parents"]) < 2
        or any(not isinstance(value, str) or not _HEX_SHA.fullmatch(value) for value in row["parents"])
    ):
        raise ValueError("report contract parents are incomplete")
    for key in ("total_commits", "non_bot_authored_commits"):
        if isinstance(row[key], bool) or not isinstance(row[key], int) or row[key] < 0:
            raise ValueError(f"report contract has invalid {key}")
    provenance = row["provenance"]
    if not isinstance(provenance, dict):
        raise ValueError("report contract provenance must be an object")
    if set(provenance) != {"bot_identity", "co_authored_by", "merged_parent", "unattributed"}:
        raise ValueError("report contract provenance buckets drifted")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in provenance.values()):
        raise ValueError("report contract provenance counts are invalid")
    if sum(provenance.values()) != row["total_commits"]:
        raise ValueError("report contract provenance does not sum to total commits")
    if row["non_bot_authored_commits"] != row["total_commits"] - provenance["bot_identity"]:
        raise ValueError("report contract non-bot count does not match bot bucket")
    if not isinstance(row["unattributed_shas"], list) or any(
        not isinstance(value, str) or not _HEX_SHA.fullmatch(value)
        for value in row["unattributed_shas"]
    ):
        raise ValueError("report contract unattributed SHAs are invalid")
    if len(row["unattributed_shas"]) != provenance["unattributed"]:
        raise ValueError("report contract unattributed SHAs do not match the bucket")
    if row["zero_unattributed_commits"] is not (not row["unattributed_shas"]):
        raise ValueError("report contract zero-unattributed conclusion drifted")
    history = row["force_promote_history"]
    if (
        not isinstance(history, dict)
        or history.get("status") != "unrecoverable"
        or not isinstance(history.get("reason"), str)
        or "last_qualification" not in history["reason"]
    ):
        raise ValueError("report contract must state unrecoverable force-promote history")


def render_contract(contract: dict[str, Any]) -> str:
    """Render the findings document solely from the checked-in contract."""
    expected = contract_sha256(contract)
    if contract.get("contract_sha256") != expected:
        raise ValueError("cannot render a contract with an invalid self-hash")
    lines = [
        "# Release authorship history",
        "",
        "<!-- Generated from scripts/release_authorship_report_contract.json; "
        f"contract_sha256={expected} -->",
        "",
        "This table is a deterministic reading of annotated release tags and their "
        "immutable merge-parent topology. `non-bot` counts commit authors whose "
        "identity is not the Blueprint bot; provenance is accepted only from the "
        "documented bot identity, an accepted `Co-authored-by` trailer, or recursive "
        "attribution of merged side-parent history.",
        "",
        "| Release | Base SHA | Candidate/tag SHA | Tag object SHA | Total | Non-bot | Bot identity | Co-authored-by | Merged parent | Unattributed | Zero unattributed | Force-promote history |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|:---:|---|",
    ]
    for row in contract["releases"]:
        provenance = row["provenance"]
        lines.append(
            "| {version} | `{base_sha}` | `{commit_sha}` | `{tag_object_sha}` | "
            "{total_commits} | {non_bot_authored_commits} | {bot_identity} | "
            "{co_authored_by} | {merged_parent} | {unattributed} | {zero} | {history} |".format(
                **row,
                bot_identity=provenance["bot_identity"],
                co_authored_by=provenance["co_authored_by"],
                merged_parent=provenance["merged_parent"],
                unattributed=provenance["unattributed"],
                zero="yes" if row["zero_unattributed_commits"] else "no",
                history=row["force_promote_history"]["status"],
            )
        )
    lines.extend(
        [
            "",
            "The zero-unattributed conclusion is the contract field shown in the "
            "table, not an inference from a blank cell. Any nonzero unattributed "
            "count is listed by SHA in the JSON contract and makes verification "
            "fail until the evidence is resolved.",
            "",
            "Per-version release-mechanic force-promote actions are historically "
            "unrecoverable: old release records retained only a mutable "
            "`last_qualification` slot, so missing historical evidence is reported "
            "as `unrecoverable`, never as zero.",
            "",
        ]
    )
    return "\n".join(lines)


def verify_contract(repo_root: Path, contract_path: Path, document_path: Path) -> tuple[bool, str]:
    try:
        contract = load_contract(contract_path)
    except ValueError as exc:
        return False, str(exc)
    contract_unattributed = _unattributed_summary(contract["releases"])
    if contract_unattributed:
        return False, f"report contract has unresolved unattributed commits: {contract_unattributed}"
    versions = [row["version"] for row in contract["releases"]]
    measured = measure_report(repo_root, versions)
    if measured.get("state") != "complete":
        return False, json.dumps(measured, sort_keys=True)
    actual_rows = measured["releases"]
    measured_unattributed = _unattributed_summary(actual_rows)
    if measured_unattributed:
        return False, f"live Git measurement has unresolved unattributed commits: {measured_unattributed}"
    expected_rows = [dict(row) for row in contract["releases"]]
    for row in expected_rows:
        row.pop("force_promote_history", None)
    if actual_rows != expected_rows:
        return False, "live Git measurement differs from the checked-in contract\n" + _diff_rows(expected_rows, actual_rows)
    try:
        rendered = render_contract(contract)
        checked_in = document_path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        return False, str(exc)
    if checked_in != rendered:
        return False, f"{document_path} is not the rendered form of the report contract"
    return True, f"verified {len(actual_rows)} release rows (contract {contract['contract_sha256']})"


def _diff_rows(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> str:
    return "expected=" + _canonical_json(expected) + "\nactual=" + _canonical_json(actual)


def _unattributed_summary(rows: Iterable[dict[str, Any]]) -> str:
    """Return a concise per-release summary when provenance remains unresolved."""
    return ", ".join(
        f"{row['version']} ({row['provenance']['unattributed']})"
        for row in rows
        if row["provenance"]["unattributed"]
    )


def _default_paths() -> tuple[Path, Path]:
    root = Path(__file__).resolve().parents[1]
    return root / "scripts" / "release_authorship_report_contract.json", root / "docs" / "findings" / "release-authorship-history.md"


def main(argv: Sequence[str] | None = None) -> int:
    contract_default, document_default = _default_paths()
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--measure", action="store_true", help="measure local Git history and print JSON")
    modes.add_argument("--render", action="store_true", help="render the checked-in contract to stdout")
    modes.add_argument("--verify", action="store_true", help="verify local history and the committed document")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Git checkout to inspect")
    parser.add_argument("--contract", type=Path, default=contract_default)
    parser.add_argument("--document", type=Path, default=document_default)
    parser.add_argument("--version", action="append", help="release tag to measure (repeatable)")
    args = parser.parse_args(argv)
    try:
        if args.measure:
            versions = args.version
            if not versions:
                contract = load_contract(args.contract)
                versions = [row["version"] for row in contract["releases"]]
            result = measure_report(args.repo, versions)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result.get("state") == "complete" else 2
        contract = load_contract(args.contract)
        if args.render:
            print(render_contract(contract), end="")
            return 0
        ok, message = verify_contract(args.repo, args.contract, args.document)
        print(message)
        return 0 if ok else 1
    except (OSError, ValueError, IncompleteHistory) as exc:
        print(f"release-authorship-report: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
