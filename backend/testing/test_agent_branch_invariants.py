"""Static-guard tests against hardcoded LTS branch literals in agent TS files.

These tests assert that the agent-side handlers and prompt generator never
reintroduce direct equality/inequality comparisons against the bare branch
literals 'main', 'automain', or 'master'.  Such comparisons gate behavior on
a hardcoded branch name instead of routing through the per-project
``promotionTarget`` payload field.

Guarded regression: before the 'agent learns project LTS branch from payload'
refactor (clusters 1-7) landed, several handlers gated test-run, baseline-
capture, and review-fix logic on ``baseBranch === 'main'``.  Projects whose
LTS branch is something other than ``main`` (e.g. ``automain``, ``production``,
``release/v2``) silently fell through those gates and skipped the test-run
step entirely.  The correct pattern is ``baseBranch === (promotionTarget ?? 'main')``
or routing through per-project config — the ``?? 'main'`` is a last-resort
default for projects that have not configured a promotion target.

Allowed idioms that must NOT trigger the guard:
  * ``?? 'main'`` / ``|| 'main'`` (nullish/logical fallback defaults)
  * ``strVal(payload.base_branch, 'main')`` (literal as default-value argument)
  * ``baseBranch === (promotionTarget ?? 'main')`` (parenthesized fallback as RHS)
  * comments and JSDoc blocks (``//``, ``*``, ``/* ... */``)
  * branch-name string literals in log messages, commit prefixes, etc.

Run standalone: pytest testing/test_agent_branch_invariants.py -v --noconftest
"""

import os
import re
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Source file paths
# ---------------------------------------------------------------------------


def _resolve_agent_src_root() -> Path | None:
    """Locate ``agent/src`` across local + Docker test environments.

    Search order (mirrors ``_resolve_prompt_ts_path`` in test_invariants.py):
      1. ``AGENT_SRC_ROOT`` env override (caller-supplied path).
      2. ``/agent/src`` (Docker volume-mount target used by
         ``scripts/test-backend.sh``).
      3. Host filesystem at ``../../agent/src`` from this test file
         (``backend/testing/`` → repo root).

    Returns the first existing directory or ``None`` if none resolve.  The
    CI backend Docker image builds from ``./backend`` only, so neither (2)
    nor (3) exists in that context and the guard tests skip — the agent
    CI lane already runs ``npx tsc --noEmit`` and the local pre-PR run via
    ``scripts/test-backend.sh`` mounts ``agent/src`` for full enforcement.
    """
    candidates: list[Path] = []
    env_override = os.getenv("AGENT_SRC_ROOT")
    if env_override:
        candidates.append(Path(env_override))
    candidates.append(Path("/agent/src"))
    candidates.append(Path(__file__).resolve().parents[2] / "agent" / "src")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


_AGENT_SRC = _resolve_agent_src_root()

_TARGET_RELATIVE = [
    Path("handlers") / "review-fix.ts",
    Path("handlers") / "specialized.ts",
    Path("handlers") / "standard.ts",
    Path("handlers") / "code-change.ts",
    Path("prompt.ts"),
]

# Files scanned by the payload.base_branch cascade guard. Every handler that
# reads ``payload.base_branch`` must route through ``resolveBaseBranch`` (or
# the equivalent ``strVal(payload.base_branch) || ... || 'main'`` cascade)
# so non-Blueprint projects honour their configured integration branch
# instead of silently defaulting to 'main'. ``index.ts`` is included because
# the role-loop also reads ``payload.base_branch`` directly.
_PAYLOAD_BASE_BRANCH_TARGET_RELATIVE = [
    Path("handlers") / "review-fix.ts",
    Path("handlers") / "specialized.ts",
    Path("handlers") / "standard.ts",
    Path("handlers") / "research-plan.ts",
    Path("handlers") / "design-doc.ts",
    Path("index.ts"),
]

_TARGET_FILES: list[Path] = (
    [_AGENT_SRC / rel for rel in _TARGET_RELATIVE] if _AGENT_SRC else []
)

_PAYLOAD_BASE_BRANCH_TARGET_FILES: list[Path] = (
    [_AGENT_SRC / rel for rel in _PAYLOAD_BASE_BRANCH_TARGET_RELATIVE]
    if _AGENT_SRC
    else []
)


# Matches a direct equality/inequality comparison against a bare branch
# literal: ``=== 'main'``, ``!== "automain"``, ``== 'master'``, etc.
_FORBIDDEN_RE = re.compile(r"""(===|!==|==|!=)\s*['"](main|automain|master)['"]""")

# Matches the ``strVal(payload.base_branch, '<fallback>')`` regression:
# a direct payload read with a default-value second argument bypasses the
# ``project_metadata.integration_branch`` cascade and silently defaults to
# the bare literal for projects whose integration branch is not 'main'.
# Correct patterns: ``resolveBaseBranch(payload, task)`` or
# ``strVal(payload.base_branch) || strVal(meta?.integration_branch) || 'main'``
# (note the absence of a second argument to strVal in either case).
_FORBIDDEN_PAYLOAD_BASE_BRANCH_DEFAULT_RE = re.compile(
    r"""strVal\s*\(\s*payload\.base_branch\s*,"""
)

# Matches parenthesized fallback expressions like ``(promotionTarget ?? 'main')``
# or ``(baseBranch || "automain")`` so they can be stripped before the forbidden
# regex runs.  We only strip the inner literal, leaving the surrounding code
# intact so unrelated comparisons on the same line remain visible.
_PAREN_FALLBACK_RE = re.compile(
    r"""\(\s*[^()]*?\s*(\?\?|\|\|)\s*['"](?:main|automain|master)['"]\s*\)"""
)


# ---------------------------------------------------------------------------
# Source-text helpers
# ---------------------------------------------------------------------------


def _strip_comments(src: str) -> list[tuple[int, str]]:
    """Return (lineno, text) pairs with comment lines removed.

    Handles:
      * ``// line comment`` — entire line skipped if the non-whitespace prefix
        starts with ``//``.  Inline trailing ``//`` comments are stripped from
        the end of the line so a literal inside the comment cannot trigger
        the guard.
      * ``/* ... */`` multi-line block comments — tracked across lines.
      * JSDoc-style ``*`` continuation lines (after a ``/**`` opener that
        terminates on a later line) — these are inside a block comment and
        therefore skipped by the block-comment tracking.  Lone ``*`` lines
        outside a block comment are conservatively treated as JSDoc and
        skipped.

    String-literal awareness is intentionally simple: we do not parse strings,
    so a ``//`` inside a string literal would be misinterpreted as a comment.
    For the agent TS files in scope this has not produced false positives;
    if it does in the future, switch to a real TS tokenizer.
    """
    result: list[tuple[int, str]] = []
    in_block = False
    for i, line in enumerate(src.splitlines(), start=1):
        stripped = line
        if in_block:
            end = stripped.find("*/")
            if end == -1:
                continue
            stripped = stripped[end + 2 :]
            in_block = False

        # Repeatedly handle ``/* ... */`` on the same line, plus an unterminated
        # ``/*`` that opens a multi-line block.
        while True:
            start = stripped.find("/*")
            if start == -1:
                break
            end = stripped.find("*/", start + 2)
            if end == -1:
                stripped = stripped[:start]
                in_block = True
                break
            stripped = stripped[:start] + stripped[end + 2 :]

        # Strip trailing ``//`` line comments.
        slash_idx = stripped.find("//")
        if slash_idx != -1:
            stripped = stripped[:slash_idx]

        lstripped = stripped.lstrip()
        # Skip empty lines, lone JSDoc continuation lines, and standalone
        # ``//`` lines (already stripped above but left empty).
        if not lstripped:
            continue
        if lstripped.startswith("*"):
            continue
        result.append((i, stripped))
    return result


def _strip_paren_fallbacks(line: str) -> str:
    """Remove parenthesized fallback expressions from a line.

    ``baseBranch === (promotionTarget ?? 'main')`` becomes
    ``baseBranch === `` after stripping, so the forbidden regex does not
    treat the fallback default as a hardcoded comparison.
    """
    prev = None
    out = line
    while prev != out:
        prev = out
        out = _PAREN_FALLBACK_RE.sub("", out)
    return out


def _scan_for_offenders(path: Path) -> list[str]:
    """Read ``path`` and return ``file:lineno: <line>`` offender strings."""
    src = path.read_text(encoding="utf-8")
    offenders: list[str] = []
    for lineno, line in _strip_comments(src):
        cleaned = _strip_paren_fallbacks(line)
        if _FORBIDDEN_RE.search(cleaned):
            offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    return offenders


def _scan_for_payload_base_branch_default(path: Path) -> list[str]:
    """Return offender strings for ``strVal(payload.base_branch, '<fallback>')``.

    This pattern bypasses the per-project integration_branch cascade and
    silently defaults to the bare literal for projects whose integration
    branch is not ``'main'``. Correct alternatives are ``resolveBaseBranch(
    payload, task)`` or ``strVal(payload.base_branch) || strVal(meta?.\
    integration_branch) || 'main'`` (note the absence of a second argument
    to ``strVal``).
    """
    src = path.read_text(encoding="utf-8")
    offenders: list[str] = []
    for lineno, line in _strip_comments(src):
        if _FORBIDDEN_PAYLOAD_BASE_BRANCH_DEFAULT_RE.search(line):
            offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    return offenders


# ---------------------------------------------------------------------------
# Guard tests
# ---------------------------------------------------------------------------


class TestAgentBranchInvariants:
    """Static guards against hardcoded LTS branch literals in the agent TS
    handlers and the prompt generator.

    Each test reads the source of the target file(s) and asserts that no
    equality/inequality comparison hardcodes ``'main'``, ``'automain'``, or
    ``'master'`` as the right-hand side.  Tests are deliberately narrow so
    they fire on the exact regression class (``baseBranch === 'main'``)
    without flagging correct fallback-default idioms
    (``baseBranch === (promotionTarget ?? 'main')``).
    """

    def test_target_files_exist(self):
        """Every scanned path must resolve.  A missing path means the file
        was renamed or moved and the guard is silently no-op'ing — fail
        loudly so a future rename surfaces the gap.

        Skipped when ``agent/src`` is not available in this test environment
        (CI backend Docker context builds from ``./backend`` only — the agent
        CI lane already runs ``npx tsc --noEmit`` against the live sources;
        local runs via ``scripts/test-backend.sh`` mount ``agent/src`` so the
        guard fires there).
        """
        if _AGENT_SRC is None:
            pytest.skip(
                "agent/src not available in this test environment "
                "(CI docker context is ./backend; run locally or via "
                "scripts/test-backend.sh from a checkout for enforcement)"
            )
        missing = [str(p) for p in _TARGET_FILES if not p.is_file()]
        assert not missing, (
            "Scanned agent source path no longer exists.  The guard would "
            "silently pass on a missing file — update the path list in this "
            "test to match the rename:\n" + "\n".join(missing)
        )

    def test_no_hardcoded_branch_comparison_in_agent_sources(self):
        """No agent TS file may contain a direct equality/inequality
        comparison against a bare ``'main'``, ``'automain'``, or ``'master'``
        string literal.

        Correct pattern: route the comparison through the per-project
        ``promotionTarget`` value, with ``?? 'main'`` as a last-resort
        fallback for projects that have not configured a promotion target::

            if (baseBranch === (promotionTarget ?? 'main')) { ... }

        Idioms that intentionally remain allowed (must not trigger):

          * ``?? 'main'`` / ``|| 'main'`` as a fallback default
          * ``strVal(payload.base_branch, 'main')`` and similar default-value
            function arguments where the literal is the 2nd+ argument
          * ``(promotionTarget ?? 'main')`` used as the RHS of a comparison —
            the parenthesized fallback expression is stripped before the
            forbidden regex runs
          * comments (``//``, ``*``, ``/* ... */``)
          * branch-name literals in log messages, commit prefixes, or other
            non-comparison contexts (these naturally do not match the
            comparison-operator regex)

        Skipped when ``agent/src`` is not available in this test environment
        (see ``test_target_files_exist`` for the resolution policy).
        """
        if _AGENT_SRC is None:
            pytest.skip(
                "agent/src not available in this test environment "
                "(CI docker context is ./backend; run locally or via "
                "scripts/test-backend.sh from a checkout for enforcement)"
            )
        all_offenders: list[str] = []
        for path in _TARGET_FILES:
            all_offenders.extend(_scan_for_offenders(path))

        assert not all_offenders, (
            "Hardcoded LTS branch comparison found in agent TS source.  "
            "Use ``baseBranch === (promotionTarget ?? 'main')`` (or route "
            "through per-project config) instead of comparing against a "
            "bare literal:\n" + "\n".join(all_offenders)
        )

    def test_no_payload_base_branch_default_in_agent_sources(self):
        """No agent TS handler may read ``payload.base_branch`` via
        ``strVal(payload.base_branch, '<fallback>')``.

        The backend stamps every claim response's
        ``project_metadata.integration_branch`` (see
        ``backend/app/routers/agent.py``).  Calling ``strVal(payload.base_\
        branch, 'main')`` bypasses that field and silently defaults to the
        bare literal for projects whose integration branch is not 'main'
        (e.g. 'master', 'production', 'develop').

        Correct patterns:

          * ``resolveBaseBranch(payload, task)`` — the preferred helper.
          * ``strVal(payload.base_branch) || strVal(meta?.integration_branch)
            || 'main'`` — the inlined cascade (note the absence of a second
            argument to ``strVal``).

        Skipped when ``agent/src`` is not available in this test environment.
        """
        if _AGENT_SRC is None:
            pytest.skip(
                "agent/src not available in this test environment "
                "(CI docker context is ./backend; run locally or via "
                "scripts/test-backend.sh from a checkout for enforcement)"
            )
        all_offenders: list[str] = []
        for path in _PAYLOAD_BASE_BRANCH_TARGET_FILES:
            if not path.is_file():
                continue
            all_offenders.extend(_scan_for_payload_base_branch_default(path))

        assert not all_offenders, (
            "``strVal(payload.base_branch, '<fallback>')`` bypasses the "
            "per-project integration_branch cascade.  Use "
            "``resolveBaseBranch(payload, task)`` (or the inlined cascade "
            "``strVal(payload.base_branch) || strVal(meta?.integration_"
            "branch) || 'main'``) instead:\n" + "\n".join(all_offenders)
        )

    def test_scanner_detects_injected_regression(self):
        """Sanity-check the scanner itself against synthetic source text so
        a future refactor that accidentally weakens the regex (e.g. anchors
        it too tightly, or strips too aggressively) is caught.

        Each ``bad`` snippet should be flagged; each ``good`` snippet must
        not be.  The asserts use synthetic strings only — no agent file is
        touched here, so this test is independent of the live source.
        """
        bad_snippets = [
            "if (baseBranch === 'main') {",
            "if (baseBranch !== 'main') return;",
            'const x = baseBranch == "automain";',
            "const y = baseBranch != 'master';",
            '  return base === "main";',
        ]
        good_snippets = [
            "if (baseBranch === (promotionTarget ?? 'main')) {",
            "if (baseBranch === (promotionTarget || 'main')) {",
            "const target = promotionTarget ?? 'main';",
            "const target = promotionTarget || 'main';",
            "// baseBranch === 'main' is the legacy check",
            " * comparison was baseBranch === 'main' before the refactor",
            "log.info('switched to main branch');",
            "const prefix = 'blueprint';",
        ]

        for snippet in bad_snippets:
            lines = _strip_comments(snippet)
            assert lines, f"Bad snippet was stripped as a comment: {snippet!r}"
            _, line = lines[0]
            cleaned = _strip_paren_fallbacks(line)
            assert _FORBIDDEN_RE.search(cleaned), (
                f"Scanner failed to flag known-bad snippet: {snippet!r}"
            )

        for snippet in good_snippets:
            lines = _strip_comments(snippet)
            if not lines:
                # Comment-only line — correctly stripped, nothing to check.
                continue
            _, line = lines[0]
            cleaned = _strip_paren_fallbacks(line)
            assert not _FORBIDDEN_RE.search(cleaned), (
                f"Scanner false-positive on known-good snippet: {snippet!r}"
            )

    def test_payload_base_branch_default_scanner(self):
        """Sanity-check the ``strVal(payload.base_branch, ...)`` scanner against
        synthetic snippets so a future tweak to the regex does not silently
        weaken the guard.
        """
        bad_snippets = [
            "const x = strVal(payload.base_branch, 'main');",
            'const y = strVal(payload.base_branch, "automain");',
            "  const z = strVal( payload.base_branch , 'master');",
        ]
        good_snippets = [
            "const x = strVal(payload.base_branch);",
            "const y = resolveBaseBranch(payload, task);",
            "const z = strVal(payload.base_branch) || strVal(meta?.integration_branch) || 'main';",
            "// strVal(payload.base_branch, 'main') was the legacy pattern",
            "const ok = strVal(payload.branch, 'main');",  # different field
        ]

        for snippet in bad_snippets:
            lines = _strip_comments(snippet)
            assert lines, f"Bad snippet was stripped as a comment: {snippet!r}"
            _, line = lines[0]
            assert _FORBIDDEN_PAYLOAD_BASE_BRANCH_DEFAULT_RE.search(line), (
                f"Scanner failed to flag known-bad snippet: {snippet!r}"
            )

        for snippet in good_snippets:
            lines = _strip_comments(snippet)
            if not lines:
                continue
            _, line = lines[0]
            assert not _FORBIDDEN_PAYLOAD_BASE_BRANCH_DEFAULT_RE.search(line), (
                f"Scanner false-positive on known-good snippet: {snippet!r}"
            )


# ---------------------------------------------------------------------------
# Static prompt-section byte-budget guard
# ---------------------------------------------------------------------------


def _extract_template_literal_bytes(name: str, src: str) -> int | None:
    """Return the byte length of a ``const NAME = `...``` template literal body.

    Walks forward from the opening backtick, honouring ``\\`` escape sequences
    and ``${...}`` expression depth, so nested template expressions and escaped
    backticks do not prematurely close the outer literal.  Returns ``None`` if
    the constant declaration is not found.
    """
    m = re.search(rf"(?:export\s+)?const\s+{re.escape(name)}\s*=\s*`", src)
    if not m:
        return None
    i = m.end()
    depth = 0
    start = i
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "`" and depth == 0:
            return i - start
        if c == "$" and i + 1 < len(src) and src[i + 1] == "{":
            depth += 1
            i += 2
            continue
        if c == "}" and depth > 0:
            depth -= 1
        i += 1
    return None


class TestPromptStaticBudget:
    """Byte-budget guard for code-change static prompt sections in prompt.ts.

    A compute experiment (2026-06-19, artifact a18629c4-9784-4df2-a18b-4be0f7a2ec11)
    measured the sections that feed ``code_change_static_tokens`` at 9 723 tokens
    (34 084 bytes) at a 3.51 chars/token ratio.  This test fires when the combined
    byte count of those sections exceeds the 12 000-token calibrated ceiling
    (42 000 bytes), ensuring prompt-size growth is caught before it affects
    live LLM cost metrics.

    Skipped in CI backend Docker builds where ``agent/src`` is not mounted —
    the agent TypeScript CI lane already covers prompt.ts at build time; local
    and ``scripts/test-backend.sh`` runs enforce the guard with the mounted source.
    """

    # Sections that together form code_change_static_tokens (experiment-verified):
    #   lifecycle_total = PREAMBLE + BLUEPRINT_CHECK_INTRO + SHARED_LIFECYCLE_REMAINDER
    #   failure_guidance = READ_THE_FAILURE_MESSAGE + SIZE_GUARDRAIL_GUIDANCE
    #                      + PRIOR_ATTEMPT_DIVERGENCE + BEYOND_SCOPE_REPORTING
    #                      + EDGES_CONSIDERED_GUIDANCE
    #   + PLAYBOOK_GUIDANCE
    # (DEFAULT_TOOL_GUIDANCE is a regular string constant, not a template literal,
    # and accounts for ~260 tokens / ~913 bytes not checked here.)
    _CODE_CHANGE_SECTIONS = [
        "SHARED_LIFECYCLE_PREAMBLE",
        "BLUEPRINT_CHECK_INTRO",
        "SHARED_LIFECYCLE_REMAINDER",
        "READ_THE_FAILURE_MESSAGE",
        "SIZE_GUARDRAIL_GUIDANCE",
        "PRIOR_ATTEMPT_DIVERGENCE",
        "BEYOND_SCOPE_REPORTING",
        "EDGES_CONSIDERED_GUIDANCE",
        "PLAYBOOK_GUIDANCE",
    ]

    # Calibrated ceiling: 12 000 tokens × 3.51 chars/token (ratio from the
    # 2026-06-19 experiment).  Baseline: 9 723 tokens / 34 084 bytes.
    _BYTE_CEILING = 42_000

    def test_code_change_static_section_budget(self):
        """Combined byte count of code-change static sections stays under
        the 12 000-token calibrated ceiling.

        Skipped when ``agent/src`` is not available in this environment.
        """
        if _AGENT_SRC is None:
            pytest.skip(
                "agent/src not available in this test environment "
                "(CI docker context is ./backend; run locally or via "
                "scripts/test-backend.sh for enforcement)"
            )
        prompt_ts = _AGENT_SRC / "prompt.ts"
        if not prompt_ts.is_file():
            pytest.skip(f"prompt.ts not found at {prompt_ts}")

        src = prompt_ts.read_text(encoding="utf-8")
        section_bytes: dict[str, int] = {}
        missing: list[str] = []

        for name in self._CODE_CHANGE_SECTIONS:
            n = _extract_template_literal_bytes(name, src)
            if n is None:
                missing.append(name)
            else:
                section_bytes[name] = n

        assert not missing, (
            "Could not locate these prompt.ts template-literal constants — "
            "update _CODE_CHANGE_SECTIONS if they were renamed:\n" + "\n".join(missing)
        )

        total = sum(section_bytes.values())
        breakdown = "\n".join(
            f"  {k}: {v:,} bytes"
            for k, v in sorted(section_bytes.items(), key=lambda x: -x[1])
        )
        assert total <= self._BYTE_CEILING, (
            f"Code-change static prompt sections exceeded the 12 000-token "
            f"byte budget ({total:,} > {self._BYTE_CEILING:,} bytes).\n"
            f"Baseline (2026-06-19): 34 084 bytes / 9 723 tokens.\n"
            f"Reduce prompt text or raise the ceiling after operator review.\n"
            f"Per-section breakdown:\n{breakdown}"
        )
