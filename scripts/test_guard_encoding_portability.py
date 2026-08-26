"""Regression pins for the Windows guard-script encoding/newline defect class.

Every guard under ``scripts/`` runs on a developer host as well as in CI, and on
the Windows developer host Python's locale codec is cp1252, not UTF-8. That one
difference broke the guards three ways, and this module pins the repair for
each:

1. **Guard output stops being UTF-8.**
   ``python scripts/generate_test_estate_ranked_queue.py --check`` emitted byte
   ``0x97`` for every em dash in its stale-queue diff -- 403 of them in the
   63 KB run that motivated this file -- so any consumer decoding that lane
   output as UTF-8 died with ``'utf-8' codec can't decode byte 0x97 in
   position 1525: invalid start byte``.

2. **The guard dies mid-report.** cp1252 cannot encode the box drawing, arrows
   and check marks that pytest/tsc/vitest output carries, so a guard echoing
   captured tool output raises ``UnicodeEncodeError`` while printing the very
   failure it found. Note that ``errors="replace"`` on the DECODE side does not
   help here: U+FFFD is itself unencodable in cp1252.

3. **Reads and writes silently change bytes.** ``subprocess`` with a bare
   ``text=True`` decodes tool output with the locale codec, and a text-mode
   write translates newlines. A Windows ``--fix`` or ``--write`` therefore
   rewrites a whole tracked artifact as CRLF to change one number.

The stream tests below force the defect on ANY host by setting
``PYTHONIOENCODING=cp1252`` on a real child process, so Linux CI pins exactly
the behaviour the Windows host needs rather than passing trivially.

Safe to run standalone: no DB, no network, no Docker.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import generate_test_estate_ranked_queue as estate_queue
import guard_suppression_receipt
import typecheck_verification_guard
from testing.check_size_limits import load_limits, write_limits
from testing.fixgate_attribution import format_offender_marker

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPOSITORY_ROOT / "scripts"

# cp1252's encoding of U+2014 EM DASH, and the byte the real guard run emitted.
LEGACY_EM_DASH = b"\x97"

# Guards a developer runs directly. Every one of them either echoes captured
# tool output or writes a tracked artifact, so every one is in the class.
GUARD_MODULES = (
    "scripts/backend_verification_guard.py",
    "scripts/generate_test_estate_ranked_queue.py",
    "scripts/guard_suppression_receipt.py",
    "scripts/merge_durations.py",
    "scripts/regenerate-durations.py",
    "scripts/sentinel_freshness.py",
    "scripts/static_release_guard.py",
    "scripts/typecheck_verification_guard.py",
    "scripts/verification_receipt.py",
    "scripts/verify.py",
    "scripts/viewport_manifest.py",
    "scripts/vitest_verification_guard.py",
    "backend/app/size_limit_policy.py",
    "backend/testing/check_size_limits.py",
    "backend/testing/impacted_tests.py",
    "backend/testing/merge_size_limits.py",
)

_SUBPROCESS_CALLS = {"run", "check_output", "check_call", "call", "Popen"}


def _called_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _open_mode(node: ast.Call) -> str:
    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
        if isinstance(node.args[1].value, str):
            return node.args[1].value
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            if isinstance(keyword.value.value, str):
                return keyword.value.value
    return "r"


def _calls_force_utf8_streams(path: Path) -> bool:
    """True when *path* contains a bare ``force_utf8_streams()`` statement."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and _called_name(node.value.func) == "force_utf8_streams"
        for node in ast.walk(tree)
    )


def _run_child(source: str, *, io_encoding: str) -> subprocess.CompletedProcess[bytes]:
    """Run *source* in a real child whose stdio codec is *io_encoding*.

    Deliberately captures BYTES: decoding them here would hide the very
    substitution under test. ``PYTHONUTF8=0`` is explicit because
    ``scripts/check-default.sh`` exports ``PYTHONUTF8=1`` -- inheriting it would
    put the child in PEP 540 UTF-8 mode and mask the locale codec this test
    reproduces.
    """
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = io_encoding
    environment["PYTHONUTF8"] = "0"
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(SCRIPTS_DIR), environment.get("PYTHONPATH", "")) if part
    )
    # 20s, not 30+: this is a helper, so no caller's @pytest.mark.timeout can
    # cover it, and the CI lane budget is 30s (backend/testing/
    # test_harness_timeout_budgets.py). An inner timeout at or above the lane
    # budget can never fire -- the harness kills the child first and the run
    # reds as an unreproducible flake. A one-line `python -c` child has orders
    # of magnitude of headroom at 20s.
    return subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        env=environment,
        timeout=20,
        check=False,
    )


class TestLegacyCodecStdoutReproduction:
    """The baseline half: prove the defect is real before pinning the fix."""

    def test_unpinned_guard_stdout_is_not_utf8(self) -> None:
        """A guard printing an em dash emits 0x97, not e2 80 94."""
        result = _run_child(
            "print('measured | ' + chr(0x2014) + ' |')", io_encoding="cp1252"
        )
        assert result.returncode == 0, result.stderr
        assert LEGACY_EM_DASH in result.stdout
        with pytest.raises(UnicodeDecodeError):
            result.stdout.decode("utf-8")

    def test_unpinned_guard_dies_printing_captured_tool_output(self) -> None:
        """cp1252 has no U+2713, so echoing tool output kills the guard."""
        result = _run_child("print('suite ' + chr(0x2713))", io_encoding="cp1252")
        assert result.returncode != 0
        assert b"UnicodeEncodeError" in result.stderr


class TestForceUtf8Streams:
    """``scripts/utf8_io.force_utf8_streams`` repairs both halves above."""

    def test_pinned_guard_stdout_is_valid_utf8(self) -> None:
        result = _run_child(
            "from utf8_io import force_utf8_streams\n"
            "force_utf8_streams()\n"
            "print('measured | ' + chr(0x2014) + ' |')",
            io_encoding="cp1252",
        )
        assert result.returncode == 0, result.stderr
        assert LEGACY_EM_DASH not in result.stdout
        assert result.stdout.decode("utf-8").strip() == "measured | — |"

    def test_pinned_guard_prints_characters_the_legacy_codec_lacks(self) -> None:
        """The guard must survive echoing pytest/tsc/vitest output verbatim."""
        result = _run_child(
            "from utf8_io import force_utf8_streams\n"
            "force_utf8_streams()\n"
            "print(chr(0x2713) + chr(0x2717) + chr(0x2190) + chr(0x2500))",
            io_encoding="cp1252",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.decode("utf-8").strip() == "✓✗←─"

    def test_force_utf8_streams_never_raises_on_an_exotic_stream(self) -> None:
        """Fail-open contract: a guard must not die because stdout is exotic."""
        result = _run_child(
            "import io, sys\n"
            "from utf8_io import force_utf8_streams\n"
            "sys.stdout = io.StringIO()\n"
            "force_utf8_streams()\n"
            "sys.stdout = sys.__stdout__\n"
            "print('survived')",
            io_encoding="cp1252",
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == b"survived"

    def test_guard_entrypoints_pin_their_own_streams(self) -> None:
        """Every guard that echoes tool output calls the pin before doing so."""
        echoing = (
            "scripts/backend_verification_guard.py",
            "scripts/generate_test_estate_ranked_queue.py",
            "scripts/guard_suppression_receipt.py",
            "scripts/regenerate-durations.py",
            "scripts/sentinel_freshness.py",
            "scripts/static_release_guard.py",
            "scripts/typecheck_verification_guard.py",
            "scripts/verification_receipt.py",
            "scripts/verify.py",
            "scripts/viewport_manifest.py",
            "scripts/vitest_verification_guard.py",
            "backend/testing/check_size_limits.py",
        )
        # A substring search would be satisfied by the ImportError fallback
        # `def force_utf8_streams()` these modules define, so match the CALL.
        missing = [
            relative
            for relative in echoing
            if not _calls_force_utf8_streams(REPOSITORY_ROOT / relative)
        ]
        assert not missing, (
            "guards echo captured tool output and must pin their own stdout to "
            f"UTF-8 in main(): {missing}\n{format_offender_marker(missing)}"
        )


@pytest.fixture(scope="module")
def legacy_byte_repository(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real git repo holding a blob whose bytes are not valid UTF-8.

    A commit MESSAGE cannot carry the byte: git transcodes a non-UTF-8 message
    to ``c2 97`` on the way in. Blob content is stored verbatim, so ``git show``
    is a real guard-shaped invocation that hands back the raw ``0x97``.
    """
    repository = tmp_path_factory.mktemp("legacy-bytes")

    # 20s for the same reason as _run_child(): a fixture-local helper sits
    # under the 30s lane budget with no marker of its own, so the inner timeout
    # has to stay strictly below it to be reachable at all.
    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=repository, capture_output=True, check=True, timeout=20
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "guard@example.invalid")
    git("config", "user.name", "Guard Test")
    (repository / "legacy.txt").write_bytes(b"cost 603.712s " + LEGACY_EM_DASH + b"\n")
    git("add", "-f", "legacy.txt")
    git("commit", "-q", "-m", "add legacy-encoded file")
    raw = subprocess.run(
        ["git", "show", "HEAD:legacy.txt"],
        cwd=repository,
        capture_output=True,
        check=True,
        timeout=20,
    ).stdout
    assert LEGACY_EM_DASH in raw, "fixture lost the non-UTF-8 byte before the test ran"
    return repository


class TestGuardSubprocessDecoding:
    """Tool output is decoded utf-8/replace: mangle a character, never crash."""

    def test_git_helper_replaces_undecodable_bytes(
        self, legacy_byte_repository: Path
    ) -> None:
        """Real ``_git`` over real non-UTF-8 bytes -- no mock anywhere.

        Both mutations of the pin fail this. Dropping ``encoding=``/``errors=``
        raises ``UnicodeDecodeError`` on a UTF-8 host and silently yields an em
        dash on a cp1252 host; pinning ``encoding="utf-8"`` strictly raises on
        both.
        """
        result = typecheck_verification_guard._git(
            legacy_byte_repository, "show", "HEAD:legacy.txt"
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "cost 603.712s �\n"

    def test_guard_subprocess_calls_pin_encoding_and_errors(self) -> None:
        """Structural sweep so the class cannot regrow one call site at a time."""
        offenders: list[str] = []
        for relative in GUARD_MODULES:
            tree = ast.parse((REPOSITORY_ROOT / relative).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if _called_name(node.func) not in _SUBPROCESS_CALLS:
                    continue
                keywords = {kw.arg for kw in node.keywords if kw.arg}
                if not keywords & {"text", "universal_newlines"}:
                    continue
                if {"encoding", "errors"} <= keywords:
                    continue
                offenders.append(f"{relative}:{node.lineno}")
        assert not offenders, (
            "text-mode subprocess output must be decoded with an explicit "
            'encoding="utf-8", errors="replace" so a guard reports a mangled '
            f"character instead of crashing on a cp1252 host: {offenders}"
            f"\n{format_offender_marker(offenders)}"
        )


def _minimal_estate_root(tmp_path: Path) -> Path:
    """The smallest tree ``render_queue`` accepts: real files, real collectors."""
    root = tmp_path / "estate-root"
    (root / "backend" / "testing").mkdir(parents=True)
    (root / "admin-ui" / "src").mkdir(parents=True)
    (root / "agent" / "src").mkdir(parents=True)
    (root / "backend" / "testing" / "test_sample.py").write_text(
        "def test_sample():\n    assert True\n", encoding="utf-8"
    )
    # Unprotected on purpose: its "Workday protection" cell renders as an em
    # dash, the character whose cp1252 byte started this whole class.
    (root / "backend" / "testing" / "test_unprotected.py").write_text(
        "def test_unprotected():\n    assert True\n", encoding="utf-8"
    )
    (root / "backend" / "testing" / "conversational_workday_manifest.json").write_text(
        json.dumps(
            {"protect": [{"path": "backend/testing/test_sample.py", "test_count": 1}]}
        ),
        encoding="utf-8",
    )
    (root / "backend" / ".test_durations").write_text(
        json.dumps({"testing/test_sample.py::test_sample": 1.5}), encoding="utf-8"
    )
    for suite in ("admin-ui", "agent"):
        (root / suite / ".test_durations.json").write_text(
            json.dumps({"files": []}), encoding="utf-8"
        )
    return root


class TestTrackedArtifactWritersStayLf:
    """A regenerated artifact must be byte-identical on Windows and in CI."""

    def test_size_limit_ledger_is_written_lf_only(self, tmp_path: Path) -> None:
        ledger = tmp_path / "module_size_limits.json"
        write_limits(str(ledger), {"_doc": "x", "app": {"main.py": 10}})
        raw = ledger.read_bytes()
        assert b"\r\n" not in raw
        assert raw.endswith(b"}\n")

    def test_size_limit_ledger_read_fails_loudly_on_undecodable_bytes(
        self, tmp_path: Path
    ) -> None:
        """Strict, NOT ``errors="replace"``.

        ``--fix`` writes back what it read, so a replacing reader would launder
        mojibake into the committed ledger. This also catches the unpinned
        reader on a cp1252 host, where ``0x97`` decodes happily to an em dash.
        """
        ledger = tmp_path / "module_size_limits.json"
        ledger.write_bytes(b'{"_doc": "legacy ' + LEGACY_EM_DASH + b'"}')
        with pytest.raises(UnicodeDecodeError):
            load_limits(str(ledger))

    def test_estate_queue_write_is_lf_only(self, tmp_path: Path) -> None:
        """Drive the real ``--write`` path over a minimal but real tree.

        Load-bearing on Windows and a no-op on Linux, which is why the
        structural sweep below backs it up on every host.
        """
        root = _minimal_estate_root(tmp_path)
        assert estate_queue.main(["--write", "--root", str(root)]) == 0
        raw = (root / estate_queue.QUEUE_PATH).read_bytes()
        assert b"\r\n" not in raw
        # The em dash in the protection column is the character that produced
        # the original 0x97; it must survive as UTF-8 in the written bytes.
        assert "—".encode() in raw

    def test_tracked_artifact_writers_suppress_newline_translation(self) -> None:
        """Structural sweep: every text write in these modules pins newline."""
        offenders: list[str] = []
        for relative in GUARD_MODULES:
            tree = ast.parse((REPOSITORY_ROOT / relative).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _called_name(node.func)
                keywords = {kw.arg for kw in node.keywords if kw.arg}
                if name == "write_text" and "newline" not in keywords:
                    offenders.append(f"{relative}:{node.lineno}")
                elif name == "open":
                    mode = _open_mode(node)
                    if "b" not in mode and "w" in mode and "newline" not in keywords:
                        offenders.append(f"{relative}:{node.lineno}")
        assert not offenders, (
            'text-mode writes must pass newline="" so a Windows regeneration '
            "does not rewrite a tracked artifact as CRLF: "
            + str(offenders)
            + "\n"
            + format_offender_marker(offenders)
        )


class TestGeneratedArtifactPathsPinLf:
    """``.gitattributes`` makes LF a property of the path, not of the writer."""

    def test_regenerated_artifacts_declare_eol_lf(self) -> None:
        attributes = (REPOSITORY_ROOT / ".gitattributes").read_text(encoding="utf-8")
        declared = {
            line.split()[0]
            for line in attributes.splitlines()
            if line.strip() and not line.lstrip().startswith("#") and "eol=lf" in line
        }
        required = {
            "test-estate-ranked-queue.md",
            "backend/.test_durations",
            "backend/testing/module_size_limits.json",
            "backend/testing/cap_bump_history.json",
            "admin-ui/.test_durations.json",
            "agent/.test_durations.json",
        }
        assert required <= declared, (
            "a hand-regenerated artifact without an eol=lf pin lands as a "
            f"whole-file CRLF diff from a Windows host: {sorted(required - declared)}"
            f"\n{format_offender_marker(sorted(required - declared))}"
        )


class TestGuardReceiptPathsAreWindowsLegal:
    """A receipt path the host cannot create is a silently empty guard gate."""

    # Illegal in an NTFS path component. ``:`` is the one that actually bit:
    # lane names carry it ("typecheck:agent"), and on Windows it opens an
    # alternate data stream, so the write raised WinError 87, the fail-open
    # handler swallowed it, and the developer-host aggregate stayed empty.
    WINDOWS_ILLEGAL = set('<>:"/\\|?*')

    @pytest.mark.parametrize(
        "lane",
        ["typecheck:agent", "typecheck:admin-ui", "vitest:agent", "backend", "size-limits"],
    )
    def test_lane_receipt_filename_is_creatable_on_windows(self, lane: str) -> None:
        filename = guard_suppression_receipt.lane_receipt_path(lane, "task").name
        assert not (set(filename) & self.WINDOWS_ILLEGAL), (
            f"lane {lane!r} maps to receipt file {filename!r}, which Windows "
            "cannot create -- the guard then reports no suppressions at all"
        )

    def test_sanitised_filename_still_round_trips_the_true_lane_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sanitising the FILENAME must not rename the lane in the aggregate."""
        monkeypatch.setenv(
            guard_suppression_receipt.RECEIPT_DIR_ENV, str(tmp_path / "receipts")
        )
        written = guard_suppression_receipt.record_classification(
            "typecheck:agent", {"state": "passed", "suppressed_failures": ["a::b"]}
        )
        assert written is not None and written.is_file()
        assert "typecheck:agent" in guard_suppression_receipt.aggregate()["lanes"]


class TestGuardRepairCommandsRunOnWindows:
    """A repair instruction naming ``python3`` is not runnable on the host."""

    def test_estate_queue_repair_commands_name_a_windows_interpreter(self) -> None:
        """The python.org installer publishes ``python.exe`` and no ``python3``."""
        sources = (
            "scripts/static_release_guard.py",
            "scripts/generate_test_estate_ranked_queue.py",
            "backend/testing/test_test_estate_ranked_queue.py",
        )
        for relative in sources:
            text = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
            if "generate_test_estate_ranked_queue.py --write" not in text:
                continue
            assert "python` on Windows" in text or "python scripts/generate" in text, (
                f"{relative} tells a developer to run a repair command without "
                "naming an interpreter that exists on the Windows host"
            )
