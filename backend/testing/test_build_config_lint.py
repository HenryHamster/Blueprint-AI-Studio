"""Tests for app.build_config_lint — the operator-warning helpers for unrecognized lint_cmd values.

`detect_linter_kind` MUST stay aligned with the canonical TypeScript logic at
agent/src/prompt.ts:969-977; if these tests drift, fix the Python regex, do
not relax the assertions.
"""

from app.build_config_lint import build_config_lint_warnings, detect_linter_kind


class TestDetectLinterKind:
    def test_bare_ruff(self):
        assert detect_linter_kind("ruff check .") == "ruff"
        assert detect_linter_kind("ruff") == "ruff"

    def test_bare_eslint(self):
        assert detect_linter_kind("eslint .") == "eslint"
        assert detect_linter_kind("eslint --fix src/") == "eslint"

    def test_bare_mypy_flake8_pyright(self):
        assert detect_linter_kind("mypy backend/") == "mypy"
        assert detect_linter_kind("flake8") == "flake8"
        assert detect_linter_kind("pyright src/") == "pyright"

    def test_python_dash_m_wrappers(self):
        assert detect_linter_kind("python -m ruff check .") == "ruff"
        assert detect_linter_kind("python3 -m ruff check .") == "ruff"
        assert detect_linter_kind("python -m mypy backend/") == "mypy"
        assert detect_linter_kind("python3 -m flake8") == "flake8"

    def test_npx_wrapper(self):
        assert detect_linter_kind("npx eslint .") == "eslint"
        assert detect_linter_kind("npx eslint --fix") == "eslint"

    def test_pnpm_exec_wrapper(self):
        assert detect_linter_kind("pnpm exec eslint .") == "eslint"

    def test_unrecognized(self):
        assert detect_linter_kind("make lint") is None
        assert detect_linter_kind("./scripts/lint.sh") is None
        assert detect_linter_kind("npm run lint") is None
        assert detect_linter_kind("yarn lint") is None

    def test_word_boundary(self):
        # Tokens that merely START with a recognized name must not match —
        # the canonical TS regex uses \b after the linter name.
        assert detect_linter_kind("ruffian") is None
        assert detect_linter_kind("eslintrc-check") is None
        assert detect_linter_kind("mypypy") is None

    def test_empty_and_non_string(self):
        assert detect_linter_kind("") is None
        assert detect_linter_kind(None) is None  # type: ignore[arg-type]
        assert detect_linter_kind(123) is None  # type: ignore[arg-type]


class TestBuildConfigLintWarnings:
    def test_empty_or_missing_config(self):
        assert build_config_lint_warnings(None) == []
        assert build_config_lint_warnings({}) == []
        assert build_config_lint_warnings({"directories": []}) == []

    def test_missing_lint_cmd_is_silent(self):
        bc = {
            "directories": [
                {"path": "./backend", "type": "python"},
                {"path": "./admin-ui", "type": "node", "lint_cmd": None},
                {"path": "./agent", "type": "node", "lint_cmd": ""},
                {"path": "./infra", "type": "other", "lint_cmd": "   "},
            ]
        }
        assert build_config_lint_warnings(bc) == []

    def test_recognized_lint_cmds_are_silent(self):
        bc = {
            "directories": [
                {"path": "./backend", "type": "python", "lint_cmd": "ruff check ."},
                {
                    "path": "./admin-ui",
                    "type": "node",
                    "lint_cmd": "npx eslint .",
                },
                {
                    "path": "./agent",
                    "type": "node",
                    "lint_cmd": "pnpm exec eslint src/",
                },
                {
                    "path": "./mypkg",
                    "type": "python",
                    "lint_cmd": "python3 -m mypy mypkg/",
                },
            ]
        }
        assert build_config_lint_warnings(bc) == []

    def test_one_unrecognized_lint_cmd_produces_one_warning(self):
        bc = {
            "directories": [
                {"path": "./backend", "type": "python", "lint_cmd": "make lint"},
                {
                    "path": "./admin-ui",
                    "type": "node",
                    "lint_cmd": "npx eslint .",
                },
            ]
        }
        warnings = build_config_lint_warnings(bc)
        assert len(warnings) == 1
        assert "./backend" in warnings[0]
        assert "make lint" in warnings[0]
        assert "ruff/eslint/mypy/flake8/pyright" in warnings[0]

    def test_warnings_preserve_directory_order(self):
        bc = {
            "directories": [
                {"path": "./a", "type": "python", "lint_cmd": "make a-lint"},
                {"path": "./b", "type": "python", "lint_cmd": "ruff check ."},
                {"path": "./c", "type": "other", "lint_cmd": "./lint.sh"},
            ]
        }
        warnings = build_config_lint_warnings(bc)
        assert len(warnings) == 2
        assert "./a" in warnings[0]
        assert "./c" in warnings[1]

    def test_non_dict_entries_are_skipped(self):
        bc = {"directories": ["not a dict", None, 42]}
        assert build_config_lint_warnings(bc) == []
