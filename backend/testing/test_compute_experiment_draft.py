"""Tests for POST /api/admin/tasks/compute-experiment/draft.

Covers:
- Well-formed idea yields an editable draft with populated contract fields.
- Missing ANTHROPIC_API_KEY returns draft_available=False gracefully.
- LLM API call failure returns draft_available=False gracefully.
- Unparseable LLM JSON returns draft_available=False gracefully.
- Missing required `idea` field returns 422.
- Endpoint does NOT persist any database rows or launch a compute job.
- DISABLE_COMPUTE_EXPERIMENT_DRAFT_LLM env flag disables draft generation.
"""

import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


TEST_IDEA = "Benchmark PyTorch vs JAX on a matrix multiplication workload to measure throughput."

_GOOD_LLM_RESPONSE = json.dumps({
    "title": "PyTorch vs JAX Matrix Multiplication Throughput",
    "prompt": "Run a matrix multiplication benchmark comparing PyTorch and JAX implementations. Measure throughput in GFLOPS.",
    "hypothesis": "JAX XLA compilation will yield higher throughput than PyTorch eager mode for large matrices.",
    "command": "python3 bench/matmul_bench.py --size 4096 --iterations 100",
    "arms": None,
    "declared_metric": None,
    "spend_cap_usd": 3.0,
    "deadline_seconds": 1800,
    "experiment": "matmul-throughput",
    "benchmark": None,
    "metric": "throughput_gflops",
    "success_criterion": {
        "metric": "throughput_gflops",
        "operator": ">=",
        "threshold": 100.0,
    },
    "accelerator": None,
    "machine_type": None,
})



class TestComputeExperimentDraftReturnsEditableContract:
    """Named test satisfying the acceptance evidence requirement."""

    @pytest.mark.asyncio
    async def test_draft_returns_editable_contract(self, client: AsyncClient):
        """A well-formed idea with a mocked Anthropic call returns a populated draft."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            with patch("app.admin._call_draft_llm", new=AsyncMock(return_value=json.loads(_GOOD_LLM_RESPONSE))):
                resp = await client.post(
                    "/api/admin/tasks/compute-experiment/draft",
                    json={"idea": TEST_IDEA},
                )

        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert data["draft_available"] is True
        assert isinstance(data["title"], str) and len(data["title"]) > 0
        assert isinstance(data["prompt"], str) and len(data["prompt"]) > 0
        assert isinstance(data["spend_cap_usd"], float) and data["spend_cap_usd"] > 0
        assert isinstance(data["deadline_seconds"], int) and data["deadline_seconds"] > 0
        assert data["command"] is not None or data["arms"] is not None
        sc = data.get("success_criterion")
        if sc is not None:
            assert "metric" in sc
            assert "operator" in sc
            assert "threshold" in sc


class TestComputeExperimentDraftFallbacks:
    @pytest.mark.asyncio
    async def test_draft_no_api_key_returns_fallback(self, client: AsyncClient):
        """When ANTHROPIC_API_KEY is absent, endpoint returns draft_available=False (no 500)."""
        env_without_key = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with patch.dict(os.environ, env_without_key, clear=True):
            resp = await client.post(
                "/api/admin/tasks/compute-experiment/draft",
                json={"idea": TEST_IDEA},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["draft_available"] is False
        assert data.get("notes") is not None

    @pytest.mark.asyncio
    async def test_draft_llm_api_error_returns_fallback(self, client: AsyncClient):
        """When the Anthropic API raises an exception, endpoint returns draft_available=False."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            with patch("app.admin._call_draft_llm", new=AsyncMock(return_value=None)):
                resp = await client.post(
                    "/api/admin/tasks/compute-experiment/draft",
                    json={"idea": TEST_IDEA},
                )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["draft_available"] is False
        assert data.get("notes") is not None

    @pytest.mark.asyncio
    async def test_draft_llm_bad_json_returns_fallback(self, client: AsyncClient):
        """When the LLM returns non-JSON, endpoint returns draft_available=False."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            with patch("app.admin._call_draft_llm", new=AsyncMock(return_value=None)):
                resp = await client.post(
                    "/api/admin/tasks/compute-experiment/draft",
                    json={"idea": TEST_IDEA},
                )
        assert resp.status_code == 200, resp.text
        assert resp.json()["draft_available"] is False

    @pytest.mark.asyncio
    async def test_draft_disabled_via_env_returns_fallback(self, client: AsyncClient):
        """When DISABLE_COMPUTE_EXPERIMENT_DRAFT_LLM=1, returns draft_available=False."""
        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "sk-test-key",
            "DISABLE_COMPUTE_EXPERIMENT_DRAFT_LLM": "1",
        }):
            resp = await client.post(
                "/api/admin/tasks/compute-experiment/draft",
                json={"idea": TEST_IDEA},
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["draft_available"] is False


class TestComputeExperimentDraftValidation:
    @pytest.mark.asyncio
    async def test_draft_missing_idea_returns_422(self, client: AsyncClient):
        """Missing required `idea` field returns HTTP 422."""
        resp = await client.post(
            "/api/admin/tasks/compute-experiment/draft",
            json={"github_repo": "owner/repo"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_draft_empty_idea_returns_422(self, client: AsyncClient):
        """Empty string `idea` returns HTTP 422 (min_length=1)."""
        resp = await client.post(
            "/api/admin/tasks/compute-experiment/draft",
            json={"idea": ""},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_draft_optional_fields_accepted(self, client: AsyncClient):
        """Optional github_repo and base_branch fields are accepted."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            with patch("app.admin._call_draft_llm", new=AsyncMock(return_value=None)):
                resp = await client.post(
                    "/api/admin/tasks/compute-experiment/draft",
                    json={
                        "idea": TEST_IDEA,
                        "github_repo": "owner/repo",
                        "base_branch": "main",
                    },
                )
        assert resp.status_code == 200


class TestComputeExperimentDraftDoesNotPersist:
    @pytest.mark.asyncio
    async def test_draft_does_not_create_task_or_compute_job(self, client: AsyncClient, db_session):
        """Draft endpoint must NOT write any Task or ComputeJob rows."""
        from app.models import Task, ComputeJob
        from sqlmodel import select

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            with patch("app.admin._call_draft_llm", new=AsyncMock(return_value=json.loads(_GOOD_LLM_RESPONSE))):
                resp = await client.post(
                    "/api/admin/tasks/compute-experiment/draft",
                    json={"idea": TEST_IDEA},
                )
        assert resp.status_code == 200

        tasks = (await db_session.execute(select(Task))).scalars().all()
        jobs = (await db_session.execute(select(ComputeJob))).scalars().all()
        assert len(tasks) == 0, "Draft endpoint must not create Task rows"
        assert len(jobs) == 0, "Draft endpoint must not create ComputeJob rows"


class TestDraftRefinementRevisesPriorDraft:
    """Multi-turn refinement: a second call with prior_draft revises the existing contract."""

    @pytest.mark.asyncio
    async def test_draft_refinement_revises_prior_draft(self, client: AsyncClient):
        """Passing prior_draft alongside a refinement idea returns a revised contract."""
        prior_draft_dict = json.loads(_GOOD_LLM_RESPONSE)

        revised_response = json.dumps({
            **prior_draft_dict,
            "title": "PyTorch vs JAX (3 Arms)",
            "command": None,
            "arms": [
                {"name": "pytorch", "command": "python3 bench/matmul_bench.py --backend pytorch"},
                {"name": "jax", "command": "python3 bench/matmul_bench.py --backend jax"},
                {"name": "jax-jit", "command": "python3 bench/matmul_bench.py --backend jax --jit"},
            ],
            "declared_metric": "throughput_gflops",
            "spend_cap_usd": 5.0,
        })

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            with patch("app.admin._call_draft_llm", new=AsyncMock(return_value=json.loads(revised_response))) as mock_llm:
                resp = await client.post(
                    "/api/admin/tasks/compute-experiment/draft",
                    json={
                        "idea": "make it 3 arms, cap at $5",
                        "prior_draft": prior_draft_dict,
                    },
                )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["draft_available"] is True

        # Verify the LLM was called with the prior_draft so refinement is context-aware.
        mock_llm.assert_called_once()
        call_args = mock_llm.call_args
        assert call_args.kwargs.get("prior_draft") == prior_draft_dict

        # Revised draft should reflect the refinement (3 arms, higher spend cap).
        assert data["arms"] is not None and len(data["arms"]) == 3
        assert data["spend_cap_usd"] == 5.0


class TestCoerceDraftUnit:
    """Unit tests for the _coerce_draft helper (no HTTP, no DB, no LLM)."""

    def test_coerce_draft_valid_input(self):
        from app.admin import _coerce_draft

        raw = {
            "title": "Test",
            "prompt": "Run test.py",
            "hypothesis": "It will pass",
            "command": "python test.py",
            "arms": None,
            "declared_metric": None,
            "spend_cap_usd": 5.0,
            "deadline_seconds": 3600,
            "experiment": "test-exp",
            "benchmark": None,
            "metric": "accuracy",
            "success_criterion": {"metric": "accuracy", "operator": ">=", "threshold": 0.9},
            "accelerator": None,
            "machine_type": None,
        }
        draft = _coerce_draft(raw)
        assert draft.draft_available is True
        assert draft.title == "Test"
        assert draft.command == "python test.py"
        assert draft.spend_cap_usd == 5.0
        assert draft.deadline_seconds == 3600
        assert draft.success_criterion is not None
        assert draft.success_criterion.metric == "accuracy"
        assert draft.success_criterion.operator == ">="
        assert draft.success_criterion.threshold == 0.9

    def test_coerce_draft_defaults_spend_and_deadline(self):
        from app.admin import _coerce_draft

        raw = {"command": "python run.py", "spend_cap_usd": -1, "deadline_seconds": 0}
        draft = _coerce_draft(raw)
        assert draft.spend_cap_usd == 2.0
        assert draft.deadline_seconds == 1800

    def test_coerce_draft_invalid_metric_name_excluded(self):
        from app.admin import _coerce_draft

        raw = {
            "command": "python run.py",
            "metric": "bad metric name!",
            "success_criterion": {"metric": "bad metric!", "operator": ">=", "threshold": 1.0},
        }
        draft = _coerce_draft(raw)
        assert draft.metric is None
        assert draft.success_criterion is None

    def test_coerce_draft_arms_mode(self):
        from app.admin import _coerce_draft

        raw = {
            "command": None,
            "arms": [
                {"name": "arm-a", "command": "python a.py"},
                {"name": "arm-b", "command": "python b.py"},
            ],
            "declared_metric": "throughput",
        }
        draft = _coerce_draft(raw)
        assert draft.command is None
        assert draft.arms is not None
        assert len(draft.arms) == 2
        assert draft.declared_metric == "throughput"

    def test_parse_draft_json_strips_fences(self):
        from app.admin import _parse_draft_json

        fenced = '```json\n{"title": "test"}\n```'
        result = _parse_draft_json(fenced)
        assert result == {"title": "test"}

    def test_parse_draft_json_bare(self):
        from app.admin import _parse_draft_json

        result = _parse_draft_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_parse_draft_json_invalid_returns_none(self):
        from app.admin import _parse_draft_json

        assert _parse_draft_json("not json at all") is None
        assert _parse_draft_json("") is None

    def test_coerce_draft_truncates_oversized_fields(self):
        """_coerce_draft truncates free-text fields that exceed their max_length bounds."""
        from app.admin import _coerce_draft

        raw = {
            "title": "T" * 300,
            "prompt": "P" * 60000,
            "hypothesis": "H" * 700,
            "command": "C" * 60000,
            "notes": None,
        }
        draft = _coerce_draft(raw)
        assert draft.title is not None and len(draft.title) == 200
        assert draft.prompt is not None and len(draft.prompt) == 50000
        assert draft.hypothesis is not None and len(draft.hypothesis) == 500
        assert draft.command is not None and len(draft.command) == 50000

    def test_coerce_draft_within_bounds_not_truncated(self):
        """_coerce_draft preserves fields that are within their max_length bounds."""
        from app.admin import _coerce_draft

        raw = {
            "title": "Short title",
            "prompt": "Short prompt",
            "hypothesis": "Short hyp",
            "command": "python run.py",
        }
        draft = _coerce_draft(raw)
        assert draft.title == "Short title"
        assert draft.prompt == "Short prompt"
        assert draft.hypothesis == "Short hyp"
        assert draft.command == "python run.py"

    def test_compute_experiment_draft_model_rejects_oversized_notes(self):
        """ComputeExperimentDraft model raises ValidationError when notes exceeds max_length."""
        from app.admin import ComputeExperimentDraft
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ComputeExperimentDraft(
                draft_available=False,
                notes="N" * 1001,
            )
